"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from .tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit
    from agno.tools.arxiv import ArxivTools
    from agno.tools.calculator import CalculatorTools
    from agno.tools.csv_toolkit import CsvTools
    from agno.tools.docker import DockerTools
    from agno.tools.duckduckgo import DuckDuckGoTools
    from agno.tools.email import EmailTools
    from agno.tools.file import FileTools
    from agno.tools.github import GithubTools
    from agno.tools.googlesearch import GoogleSearchTools
    from agno.tools.jina import JinaReaderTools
    from agno.tools.newspaper import NewspaperTools
    from agno.tools.pandas import PandasTools
    from agno.tools.python import PythonTools
    from agno.tools.reddit import RedditTools
    from agno.tools.shell import ShellTools
    from agno.tools.slack import SlackTools
    from agno.tools.tavily import TavilyTools
    from agno.tools.telegram import TelegramTools
    from agno.tools.website import WebsiteTools
    from agno.tools.wikipedia import WikipediaTools
    from agno.tools.x import XTools
    from agno.tools.yfinance import YFinanceTools
    from agno.tools.youtube import YouTubeTools

    from mindroom.custom_tools.gmail import GmailTools

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
    icon_color="text-gray-600",  # Calculator gray
    docs_url="https://docs.agno.com/tools/toolkits/local/calculator",
)
def calculator_tools() -> type[CalculatorTools]:
    """Return calculator tools for mathematical operations."""
    from agno.tools.calculator import CalculatorTools  # noqa: PLC0415

    return CalculatorTools


@register_tool_with_metadata(
    name="file",
    display_name="File Operations",
    description="Read, write, and manage files",
    category=ToolCategory.DEVELOPMENT,
    icon="Folder",
    icon_color="text-yellow-600",
    docs_url="https://docs.agno.com/tools/toolkits/local/file",
)
def file_tools() -> type[FileTools]:
    """Return file tools for file system operations."""
    from agno.tools.file import FileTools  # noqa: PLC0415

    return FileTools


@register_tool_with_metadata(
    name="shell",
    display_name="Shell Commands",
    description="Execute shell commands and scripts",
    category=ToolCategory.DEVELOPMENT,
    icon="Terminal",
    icon_color="text-green-500",
    docs_url="https://docs.agno.com/tools/toolkits/local/shell",
)
def shell_tools() -> type[ShellTools]:
    """Return shell tools for command execution."""
    from agno.tools.shell import ShellTools  # noqa: PLC0415

    return ShellTools


@register_tool_with_metadata(
    name="python",
    display_name="Python Execution",
    description="Execute Python code in a sandboxed environment",
    category=ToolCategory.DEVELOPMENT,
    icon="Code",
    icon_color="text-blue-500",
    docs_url="https://docs.agno.com/tools/toolkits/local/python",
)
def python_tools() -> type[PythonTools]:
    """Return Python tools for code execution."""
    from agno.tools.python import PythonTools  # noqa: PLC0415

    return PythonTools


@register_tool_with_metadata(
    name="docker",
    display_name="Docker",
    description="Manage Docker containers and images",
    category=ToolCategory.DEVELOPMENT,
    icon="FaDocker",
    icon_color="text-blue-400",
    dependencies=["docker"],
    docs_url="https://docs.agno.com/tools/toolkits/local/docker",
)
def docker_tools() -> type[DockerTools]:
    """Return Docker tools for container management."""
    from agno.tools.docker import DockerTools  # noqa: PLC0415

    return DockerTools


@register_tool_with_metadata(
    name="github",
    display_name="GitHub",
    description="Repository and issue management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGithub",
    icon_color="text-gray-800",  # GitHub black
    config_fields=[
        ConfigField(
            name="GITHUB_ACCESS_TOKEN",
            label="GitHub Access Token",
            type="password",
            required=True,
            placeholder="ghp_...",
            description="GitHub personal access token with required permissions",
        ),
    ],
    dependencies=["PyGithub"],
    docs_url="https://docs.agno.com/tools/toolkits/others/github",
)
def github_tools() -> type[GithubTools]:
    """Return GitHub tools for repository management."""
    from agno.tools.github import GithubTools  # noqa: PLC0415

    return GithubTools


# Research & Data Tools
@register_tool_with_metadata(
    name="csv",
    display_name="CSV Files",
    description="Read and analyze CSV data with DuckDB",
    category=ToolCategory.RESEARCH,
    icon="FileText",
    icon_color="text-blue-600",
    dependencies=["duckdb"],
    docs_url="https://docs.agno.com/tools/toolkits/database/csv",
)
def csv_tools() -> type[CsvTools]:
    """Return CSV tools for data processing."""
    from agno.tools.csv_toolkit import CsvTools  # noqa: PLC0415

    return CsvTools


@register_tool_with_metadata(
    name="arxiv",
    display_name="arXiv",
    description="Search and retrieve academic papers",
    category=ToolCategory.RESEARCH,
    icon="Book",
    icon_color="text-red-600",
    dependencies=["arxiv", "pypdf"],
    docs_url="https://docs.agno.com/tools/toolkits/search/arxiv",
)
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools  # noqa: PLC0415

    return ArxivTools


@register_tool_with_metadata(
    name="duckduckgo",
    display_name="DuckDuckGo",
    description="Web search without tracking",
    category=ToolCategory.RESEARCH,
    icon="Search",
    icon_color="text-orange-500",
    dependencies=["duckduckgo-search"],
    docs_url="https://docs.agno.com/tools/toolkits/search/duckduckgo",
)
def duckduckgo_tools() -> type[DuckDuckGoTools]:
    """Return DuckDuckGo tools for web search."""
    from agno.tools.duckduckgo import DuckDuckGoTools  # noqa: PLC0415

    return DuckDuckGoTools


@register_tool_with_metadata(
    name="wikipedia",
    display_name="Wikipedia",
    description="Search encyclopedia articles",
    category=ToolCategory.RESEARCH,
    icon="Globe",
    icon_color="text-gray-700",
    dependencies=["wikipedia"],
    docs_url="https://docs.agno.com/tools/toolkits/search/wikipedia",
)
def wikipedia_tools() -> type[WikipediaTools]:
    """Return Wikipedia tools for encyclopedia research."""
    from agno.tools.wikipedia import WikipediaTools  # noqa: PLC0415

    return WikipediaTools


@register_tool_with_metadata(
    name="newspaper",
    display_name="News Articles",
    description="Extract and analyze news articles from URLs",
    category=ToolCategory.RESEARCH,
    icon="Newspaper",
    icon_color="text-gray-600",
    dependencies=["newspaper3k"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/newspaper",
)
def newspaper_tools() -> type[NewspaperTools]:
    """Return newspaper tools for article extraction."""
    from agno.tools.newspaper import NewspaperTools  # noqa: PLC0415

    return NewspaperTools


@register_tool_with_metadata(
    name="yfinance",
    display_name="Yahoo Finance",
    description="Stock market data and financial information",
    category=ToolCategory.RESEARCH,
    icon="TrendingUp",
    icon_color="text-green-600",
    dependencies=["yfinance"],
    docs_url="https://docs.agno.com/tools/toolkits/others/yfinance",
)
def yfinance_tools() -> type[YFinanceTools]:
    """Return Yahoo Finance tools for financial data."""
    from agno.tools.yfinance import YFinanceTools  # noqa: PLC0415

    return YFinanceTools


@register_tool_with_metadata(
    name="homeassistant",
    display_name="Home Assistant",
    description="Control and monitor smart home devices",
    category=ToolCategory.SMART_HOME,
    icon="Home",
    icon_color="text-blue-500",
    dependencies=["httpx"],
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    config_fields=[
        ConfigField(
            name="HOMEASSISTANT_URL",
            label="Home Assistant URL",
            type="url",
            required=True,
            placeholder="http://homeassistant.local:8123",
            description="URL to your Home Assistant instance",
        ),
        ConfigField(
            name="HOMEASSISTANT_TOKEN",
            label="Access Token",
            type="password",
            required=True,
            placeholder="Bearer token",
            description="Long-lived access token from Home Assistant",
        ),
    ],
    docs_url="https://www.home-assistant.io/integrations/",
)
def homeassistant_tools() -> type[Toolkit]:
    """Return Home Assistant tools for smart home control."""
    from mindroom.custom_tools.homeassistant import HomeAssistantTools  # noqa: PLC0415

    return HomeAssistantTools


@register_tool_with_metadata(
    name="pandas",
    display_name="Data Analysis",
    description="Pandas data manipulation and analysis",
    category=ToolCategory.RESEARCH,
    icon="Database",
    icon_color="text-purple-600",
    dependencies=["pandas"],
    docs_url="https://docs.agno.com/tools/toolkits/database/pandas",
)
def pandas_tools() -> type[PandasTools]:
    """Return Pandas tools for data analysis."""
    from agno.tools.pandas import PandasTools  # noqa: PLC0415

    return PandasTools


@register_tool_with_metadata(
    name="tavily",
    display_name="Tavily Search",
    description="Advanced AI-powered web search engine",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Search",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="TAVILY_API_KEY",
            label="Tavily API Key",
            type="password",
            required=True,
            placeholder="tvly-...",
            description="Your Tavily API key for AI-powered search",
        ),
    ],
    dependencies=["tavily-python"],
    docs_url="https://docs.agno.com/tools/toolkits/search/tavily",
)
def tavily_tools() -> type[TavilyTools]:
    """Return Tavily tools for AI-powered search."""
    from agno.tools.tavily import TavilyTools  # noqa: PLC0415

    return TavilyTools


@register_tool_with_metadata(
    name="googlesearch",
    display_name="Google Search",
    description="Search the web using Google",
    category=ToolCategory.RESEARCH,
    icon="Search",
    icon_color="text-blue-500",
    dependencies=["googlesearch-python", "pycountry"],
    docs_url="https://docs.agno.com/tools/toolkits/search/googlesearch",
)
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web queries."""
    from agno.tools.googlesearch import GoogleSearchTools  # noqa: PLC0415

    return GoogleSearchTools


@register_tool_with_metadata(
    name="website",
    display_name="Website Reader",
    description="Extract and analyze content from websites",
    category=ToolCategory.RESEARCH,
    icon="Globe",
    icon_color="text-indigo-500",
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/website",
)
def website_tools() -> type[WebsiteTools]:
    """Return website tools for web scraping."""
    from agno.tools.website import WebsiteTools  # noqa: PLC0415

    return WebsiteTools


@register_tool_with_metadata(
    name="jina",
    display_name="Jina Reader",
    description="Advanced content extraction and processing",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FileText",
    icon_color="text-pink-500",
    config_fields=[
        ConfigField(
            name="JINA_API_KEY",
            label="Jina API Key",
            type="password",
            required=True,
            placeholder="jina_...",
            description="Your Jina API key for web content extraction",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/jina_reader",
)
def jina_tools() -> type[JinaReaderTools]:
    """Return Jina tools for document reading."""
    from agno.tools.jina import JinaReaderTools  # noqa: PLC0415

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
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="SMTP_HOST",
            label="SMTP Host",
            type="text",
            required=True,
            placeholder="smtp.gmail.com",
            description="SMTP server hostname",
        ),
        ConfigField(
            name="SMTP_PORT",
            label="SMTP Port",
            type="number",
            required=True,
            default=587,
            placeholder="587",
            description="SMTP server port",
            validation={"min": 1, "max": 65535},
        ),
        ConfigField(
            name="SMTP_USERNAME",
            label="Username",
            type="text",
            required=True,
            placeholder="your-email@example.com",
            description="Email account username",
        ),
        ConfigField(
            name="SMTP_PASSWORD",
            label="Password",
            type="password",
            required=True,
            placeholder="Enter password or app-specific password",
            description="Email account password",
        ),
    ],
    docs_url="https://docs.agno.com/tools/toolkits/social/email",
)
def email_tools() -> type[EmailTools]:
    """Return email tools for message handling."""
    from agno.tools.email import EmailTools  # noqa: PLC0415

    return EmailTools


@register_tool_with_metadata(
    name="telegram",
    display_name="Telegram",
    description="Send and receive Telegram messages",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTelegram",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="TELEGRAM_TOKEN",
            label="Bot Token",
            type="password",
            required=True,
            placeholder="123456:ABC-DEF...",
            description="Your Telegram bot API token",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://core.telegram.org/bots/api",
)
def telegram_tools() -> type[TelegramTools]:
    """Return Telegram tools for messaging integration."""
    from agno.tools.telegram import TelegramTools  # noqa: PLC0415

    return TelegramTools


@register_tool_with_metadata(
    name="slack",
    display_name="Slack",
    description="Send messages and manage channels",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSlack",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="SLACK_TOKEN",
            label="Slack Token",
            type="password",
            required=True,
            placeholder="xoxb-...",
            description="Your Slack bot token",
        ),
    ],
    dependencies=["slack-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/slack",
)
def slack_tools() -> type[SlackTools]:
    """Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools  # noqa: PLC0415

    return SlackTools


# Email Category
@register_tool_with_metadata(
    name="gmail",
    display_name="Gmail",
    description="Read, search, and manage Gmail emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    icon="FaGoogle",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="GOOGLE_CLIENT_ID",
            label="Client ID",
            type="text",
            required=True,
            placeholder="123456789.apps.googleusercontent.com",
            description="Google OAuth client ID",
        ),
        ConfigField(
            name="GOOGLE_CLIENT_SECRET",
            label="Client Secret",
            type="password",
            required=True,
            placeholder="GOCSPX-...",
            description="Google OAuth client secret",
        ),
        ConfigField(
            name="GOOGLE_PROJECT_ID",
            label="Project ID",
            type="text",
            required=True,
            placeholder="my-project-123456",
            description="Google Cloud project ID",
        ),
        ConfigField(
            name="GOOGLE_REDIRECT_URI",
            label="Redirect URI",
            type="url",
            required=True,
            placeholder="http://localhost:8080/callback",
            description="OAuth redirect URI",
        ),
    ],
    dependencies=["google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2"],
    docs_url="https://docs.agno.com/tools/toolkits/social/gmail",
)
def gmail_tools() -> type[GmailTools]:
    """Gmail tools using MindRoom's Gmail wrapper."""
    from mindroom.custom_tools.gmail import GmailTools  # noqa: PLC0415

    logger.info("Using MindRoom's Gmail wrapper with unified credentials")
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
    icon_color="text-orange-600",
    config_fields=[
        ConfigField(
            name="REDDIT_CLIENT_ID",
            label="Client ID",
            type="text",
            required=True,
            placeholder="Reddit app client ID",
            description="Reddit application client ID",
        ),
        ConfigField(
            name="REDDIT_CLIENT_SECRET",
            label="Client Secret",
            type="password",
            required=True,
            placeholder="Reddit app client secret",
            description="Reddit application client secret",
        ),
        ConfigField(
            name="REDDIT_USERNAME",
            label="Username",
            type="text",
            required=True,
            placeholder="your_reddit_username",
            description="Your Reddit username",
        ),
        ConfigField(
            name="REDDIT_PASSWORD",
            label="Password",
            type="password",
            required=True,
            placeholder="Your Reddit password",
            description="Your Reddit password",
        ),
    ],
    dependencies=["praw"],
    docs_url=None,
)
def reddit_tools() -> type[RedditTools]:
    """Reddit tools for browsing and searching Reddit."""
    from agno.tools.reddit import RedditTools  # noqa: PLC0415

    return RedditTools


@register_tool_with_metadata(
    name="twitter",
    display_name="Twitter/X",
    description="Post tweets and search Twitter",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaTwitter",
    icon_color="text-blue-400",
    config_fields=[
        ConfigField(
            name="X_BEARER_TOKEN",
            label="Bearer Token",
            type="password",
            required=True,
            placeholder="Bearer token from X/Twitter",
            description="X/Twitter API Bearer token",
        ),
        ConfigField(
            name="X_CONSUMER_KEY",
            label="Consumer Key",
            type="text",
            required=True,
            placeholder="Consumer key from X/Twitter",
            description="X/Twitter API consumer key",
        ),
        ConfigField(
            name="X_CONSUMER_SECRET",
            label="Consumer Secret",
            type="password",
            required=True,
            placeholder="Consumer secret from X/Twitter",
            description="X/Twitter API consumer secret",
        ),
        ConfigField(
            name="X_ACCESS_TOKEN",
            label="Access Token",
            type="password",
            required=True,
            placeholder="Access token from X/Twitter",
            description="X/Twitter API access token",
        ),
        ConfigField(
            name="X_ACCESS_TOKEN_SECRET",
            label="Access Token Secret",
            type="password",
            required=True,
            placeholder="Access token secret from X/Twitter",
            description="X/Twitter API access token secret",
        ),
    ],
    dependencies=["tweepy"],
    docs_url="https://docs.agno.com/tools/toolkits/social/x",
)
def twitter_tools() -> type[XTools]:
    """Twitter/X tools for posting and searching tweets."""
    from agno.tools.x import XTools  # noqa: PLC0415

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
    icon_color="text-red-600",
    dependencies=["youtube-transcript-api"],
    docs_url="https://docs.agno.com/tools/toolkits/others/youtube",
)
def youtube_tools() -> type[YouTubeTools]:
    """YouTube tools for searching and getting video information."""
    from agno.tools.youtube import YouTubeTools  # noqa: PLC0415

    return YouTubeTools


# Coming Soon Tools - These are planned integrations that are not yet implemented
# They raise NotImplementedError but provide metadata for the UI


@register_tool_with_metadata(
    name="outlook",
    display_name="Microsoft Outlook",
    description="Email and calendar integration",
    category=ToolCategory.EMAIL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaMicrosoft",
    icon_color="text-blue-600",
)
def outlook_tools() -> type[Toolkit]:
    """Outlook integration - coming soon."""
    msg = "Outlook integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="yahoo_mail",
    display_name="Yahoo Mail",
    description="Email and calendar access",
    category=ToolCategory.EMAIL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaYahoo",
    icon_color="text-purple-600",
)
def yahoo_mail_tools() -> type[Toolkit]:
    """Yahoo Mail integration - coming soon."""
    msg = "Yahoo Mail integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="google_calendar",
    display_name="Google Calendar",
    description="Manage calendar events and schedules",
    category=ToolCategory.EMAIL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaGoogle",
    icon_color="text-blue-500",
)
def google_calendar_tools() -> type[Toolkit]:
    """Google Calendar integration - coming soon."""
    msg = "Google Calendar integration is coming soon"
    raise NotImplementedError(msg)


# Shopping integrations (coming soon)
@register_tool_with_metadata(
    name="amazon",
    display_name="Amazon",
    description="Search products and track orders",
    category=ToolCategory.SHOPPING,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaAmazon",
    icon_color="text-orange-500",
)
def amazon_tools() -> type[Toolkit]:
    """Amazon integration - coming soon."""
    msg = "Amazon integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="walmart",
    display_name="Walmart",
    description="Product search and price tracking",
    category=ToolCategory.SHOPPING,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="SiWalmart",
    icon_color="text-blue-500",
)
def walmart_tools() -> type[Toolkit]:
    """Walmart integration - coming soon."""
    msg = "Walmart integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="ebay",
    display_name="eBay",
    description="Auction monitoring and bidding",
    category=ToolCategory.SHOPPING,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaEbay",
    icon_color="text-blue-500",  # eBay blue
)
def ebay_tools() -> type[Toolkit]:
    """EBay integration - coming soon."""
    msg = "eBay integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="target",
    display_name="Target",
    description="Product search and availability",
    category=ToolCategory.SHOPPING,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="SiTarget",
    icon_color="text-red-600",
)
def target_tools() -> type[Toolkit]:
    """Target integration - coming soon."""
    msg = "Target integration is coming soon"
    raise NotImplementedError(msg)


# Entertainment integrations (coming soon)
@register_tool_with_metadata(
    name="netflix",
    display_name="Netflix",
    description="Track watch history and get recommendations",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="SiNetflix",
    icon_color="text-red-600",
)
def netflix_tools() -> type[Toolkit]:
    """Netflix integration - coming soon."""
    msg = "Netflix integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="spotify",
    display_name="Spotify",
    description="Music streaming and playlist management",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaSpotify",
    icon_color="text-green-500",
)
def spotify_tools() -> type[Toolkit]:
    """Spotify integration - coming soon."""
    msg = "Spotify integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="apple_music",
    display_name="Apple Music",
    description="Library and playlist management",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaApple",
    icon_color="text-gray-800",
)
def apple_music_tools() -> type[Toolkit]:
    """Apple Music integration - coming soon."""
    msg = "Apple Music integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="hbo",
    display_name="HBO Max",
    description="Watch history and content discovery",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="SiHbo",
    icon_color="text-purple-600",  # HBO purple
)
def hbo_tools() -> type[Toolkit]:
    """HBO Max integration - coming soon."""
    msg = "HBO Max integration is coming soon"
    raise NotImplementedError(msg)


# Social media integrations (coming soon)
@register_tool_with_metadata(
    name="facebook",
    display_name="Facebook",
    description="Access posts and pages",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaFacebook",
    icon_color="text-blue-600",
)
def facebook_tools() -> type[Toolkit]:
    """Facebook integration - coming soon."""
    msg = "Facebook integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="instagram",
    display_name="Instagram",
    description="View posts and stories",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaInstagram",
    icon_color="text-pink-600",
)
def instagram_tools() -> type[Toolkit]:
    """Instagram integration - coming soon."""
    msg = "Instagram integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="linkedin",
    display_name="LinkedIn",
    description="Professional network access",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaLinkedin",
    icon_color="text-blue-700",
)
def linkedin_tools() -> type[Toolkit]:
    """LinkedIn integration - coming soon."""
    msg = "LinkedIn integration is coming soon"
    raise NotImplementedError(msg)


# Development tools (coming soon)
@register_tool_with_metadata(
    name="gitlab",
    display_name="GitLab",
    description="Code and CI/CD management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaGitlab",
    icon_color="text-orange-600",
)
def gitlab_tools() -> type[Toolkit]:
    """GitLab integration - coming soon."""
    msg = "GitLab integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="dropbox",
    display_name="Dropbox",
    description="File storage and sharing",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaDropbox",
    icon_color="text-blue-600",
)
def dropbox_tools() -> type[Toolkit]:
    """Dropbox integration - coming soon."""
    msg = "Dropbox integration is coming soon"
    raise NotImplementedError(msg)


# Information tools (coming soon)
@register_tool_with_metadata(
    name="goodreads",
    display_name="Goodreads",
    description="Book tracking and recommendations",
    category=ToolCategory.INFORMATION,
    status=ToolStatus.COMING_SOON,
    setup_type=SetupType.COMING_SOON,
    icon="FaGoodreads",
    icon_color="text-amber-700",
)
def goodreads_tools() -> type[Toolkit]:
    """Goodreads integration - coming soon."""
    msg = "Goodreads integration is coming soon"
    raise NotImplementedError(msg)


@register_tool_with_metadata(
    name="imdb",
    display_name="IMDb",
    description="Movie and TV show information",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Film",
    icon_color="text-yellow-500",
    config_fields=[
        ConfigField(
            name="OMDB_API_KEY",
            label="OMDb API Key",
            type="password",
            required=True,
            placeholder="Enter your OMDb API key",
            description="Your OMDb API key for movie and TV show information",
        ),
    ],
    helper_text="Get a free API key from [OMDb API website](http://www.omdbapi.com/apikey.aspx)",
    docs_url="http://www.omdbapi.com/",
)
def imdb_tools() -> type[Toolkit]:
    """IMDb integration - coming soon."""
    msg = "IMDb integration is coming soon"
    raise NotImplementedError(msg)


def get_tool_by_name(tool_name: str) -> Toolkit:
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
        msg = f"Unknown tool: {tool_name}. Available tools: {available}"
        raise ValueError(msg)

    try:
        tool_factory = TOOL_REGISTRY[tool_name]
        tool_class = tool_factory()
        return tool_class()
    except ImportError as e:
        logger.warning(f"Could not import tool '{tool_name}': {e}")
        logger.warning(f"Make sure the required dependencies are installed for {tool_name}")
        raise
