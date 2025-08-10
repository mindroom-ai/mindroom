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


# Development Tools
@register_tool_with_metadata(
    name="calculator",
    display_name="Calculator",
    description="Mathematical calculations and expressions",
    category=ToolCategory.DEVELOPMENT,
    icon="Calculator",
    docs_url="https://docs.agno.com/tools/toolkits/local/calculator",
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
    docs_url="https://docs.agno.com/tools/toolkits/local/file",
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
    docs_url="https://docs.agno.com/tools/toolkits/local/shell",
)
def shell_tools() -> type:
    from agno.tools.shell import ShellTools

    return ShellTools


@register_tool_with_metadata(
    name="python",
    display_name="Python Execution",
    description="Execute Python code in a sandboxed environment",
    category=ToolCategory.DEVELOPMENT,
    icon="Code",
    docs_url="https://docs.agno.com/tools/toolkits/local/python",
)
def python_tools() -> type:
    from agno.tools.python import PythonTools

    return PythonTools


@register_tool_with_metadata(
    name="docker",
    display_name="Docker",
    description="Manage Docker containers and images",
    category=ToolCategory.DEVELOPMENT,
    icon="FaDocker",
    dependencies=["docker"],
    docs_url="https://docs.agno.com/tools/toolkits/local/docker",
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
    dependencies=["PyGithub"],
    docs_url="https://docs.agno.com/tools/toolkits/others/github",
)
def github_tools() -> type:
    from agno.tools.github import GithubTools

    return GithubTools


# Research & Data Tools
@register_tool_with_metadata(
    name="csv",
    display_name="CSV Files",
    description="Read and analyze CSV data with DuckDB",
    category=ToolCategory.RESEARCH,
    icon="FileText",
    dependencies=["duckdb"],
    docs_url="https://docs.agno.com/tools/toolkits/database/csv",
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
    dependencies=["arxiv", "pypdf"],
    docs_url="https://docs.agno.com/tools/toolkits/search/arxiv",
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
    dependencies=["duckduckgo-search"],
    docs_url="https://docs.agno.com/tools/toolkits/search/duckduckgo",
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
    dependencies=["wikipedia-api"],
    docs_url="https://docs.agno.com/tools/toolkits/search/wikipedia",
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
    dependencies=["newspaper3k"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/newspaper",
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
    dependencies=["yfinance"],
    docs_url="https://docs.agno.com/tools/toolkits/others/yfinance",
)
def yfinance_tools() -> type:
    from agno.tools.yfinance import YFinanceTools

    return YFinanceTools


@register_tool_with_metadata(
    name="pandas",
    display_name="Data Analysis",
    description="Pandas data manipulation and analysis",
    category=ToolCategory.RESEARCH,
    icon="Database",
    dependencies=["pandas"],
    docs_url="https://docs.agno.com/tools/toolkits/database/pandas",
)
def pandas_tools() -> type:
    from agno.tools.pandas import PandasTools

    return PandasTools


@register_tool_with_metadata(
    name="tavily",
    display_name="Tavily Search",
    description="Advanced AI-powered web search engine",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Search",
    requires_config=["TAVILY_API_KEY"],
    dependencies=["tavily-python"],
    docs_url="https://docs.agno.com/tools/toolkits/search/tavily",
)
def tavily_tools() -> type:
    from agno.tools.tavily import TavilyTools

    return TavilyTools


@register_tool_with_metadata(
    name="googlesearch",
    display_name="Google Search",
    description="Search the web using Google",
    category=ToolCategory.RESEARCH,
    icon="Search",
    dependencies=["googlesearch-python", "pycountry"],
    docs_url="https://docs.agno.com/tools/toolkits/search/googlesearch",
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
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/website",
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
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/jina_reader",
)
def jina_tools() -> type:
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools


# Communication Tools
@register_tool_with_metadata(
    name="email",
    display_name="Email",
    description="Send emails via SMTP",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Mail",
    requires_config=["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD"],
    docs_url="https://docs.agno.com/tools/toolkits/social/email",
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
    requires_config=["TELEGRAM_TOKEN"],
    dependencies=["httpx"],
    docs_url=None,
)
def telegram_tools() -> type:
    from agno.tools.telegram import TelegramTools

    return TelegramTools


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
    docs_url="https://docs.agno.com/tools/toolkits/social/slack",
)
def slack_tools() -> type[Toolkit]:
    """Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools


# Email Category
@register_tool_with_metadata(
    name="gmail",
    display_name="Gmail",
    description="Read, search, and manage Gmail emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.OAUTH,
    icon="FaGoogle",
    requires_config=["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_PROJECT_ID", "GOOGLE_REDIRECT_URI"],
    dependencies=["google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2"],
    docs_url="https://docs.agno.com/tools/toolkits/social/gmail",
)
def gmail_tools() -> type[Toolkit]:
    """Gmail tools using Agno's native Gmail toolkit."""
    from agno.tools.gmail import GmailTools

    logger.info("Using Agno's native Gmail toolkit")
    return GmailTools


# Social Media Tools
@register_tool_with_metadata(
    name="reddit",
    display_name="Reddit",
    description="Browse subreddits and search posts",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaReddit",
    requires_config=["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"],
    dependencies=["praw"],
    docs_url=None,
)
def reddit_tools() -> type[Toolkit]:
    """Reddit tools for browsing and searching Reddit."""
    from agno.tools.reddit import RedditTools

    return RedditTools


@register_tool_with_metadata(
    name="twitter",
    display_name="Twitter/X",
    description="Post tweets and search Twitter",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTwitter",
    requires_config=[
        "X_BEARER_TOKEN",
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ],
    dependencies=["tweepy"],
    docs_url="https://docs.agno.com/tools/toolkits/social/x",
)
def twitter_tools() -> type[Toolkit]:
    """Twitter/X tools for posting and searching tweets."""
    from agno.tools.x import XTools

    return XTools


# Entertainment Tools
@register_tool_with_metadata(
    name="youtube",
    display_name="YouTube",
    description="Search videos and get transcripts",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaYoutube",
    dependencies=["youtube-transcript-api"],
    docs_url="https://docs.agno.com/tools/toolkits/others/youtube",
)
def youtube_tools() -> type[Toolkit]:
    """YouTube tools for searching and getting video information."""
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools


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
