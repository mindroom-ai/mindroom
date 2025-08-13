"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

from .arxiv import arxiv_tools
from .calculator import calculator_tools
from .csv import csv_tools
from .discord import discord_tools
from .docker import docker_tools
from .duckdb import duckdb_tools
from .duckduckgo import duckduckgo_tools
from .email import email_tools
from .exa import exa_tools
from .file import file_tools
from .firecrawl import firecrawl_tools
from .github import github_tools
from .googlesearch import googlesearch_tools
from .hackernews import hackernews_tools
from .jina import jina_tools
from .newspaper import newspaper_tools
from .openai import openai_tools
from .pandas import pandas_tools
from .pubmed import pubmed_tools
from .python import python_tools
from .reddit import reddit_tools
from .resend import resend_tools
from .serpapi import serpapi_tools
from .shell import shell_tools
from .slack import slack_tools
from .sleep import sleep_tools
from .sql import sql_tools
from .tavily import tavily_tools
from .telegram import telegram_tools
from .twilio import twilio_tools
from .website import website_tools
from .whatsapp import whatsapp_tools
from .wikipedia import wikipedia_tools
from .x import x_tools
from .yfinance import yfinance_tools

if TYPE_CHECKING:
    from agno.tools import Toolkit
    from agno.tools.youtube import YouTubeTools

    from mindroom.custom_tools.gmail import GmailTools


__all__ = [
    "arxiv_tools",
    "calculator_tools",
    "csv_tools",
    "discord_tools",
    "docker_tools",
    "duckdb_tools",
    "duckduckgo_tools",
    "email_tools",
    "exa_tools",
    "file_tools",
    "firecrawl_tools",
    "github_tools",
    "googlesearch_tools",
    "hackernews_tools",
    "jina_tools",
    "newspaper_tools",
    "openai_tools",
    "pandas_tools",
    "pubmed_tools",
    "python_tools",
    "reddit_tools",
    "resend_tools",
    "serpapi_tools",
    "shell_tools",
    "slack_tools",
    "sleep_tools",
    "sql_tools",
    "tavily_tools",
    "telegram_tools",
    "twilio_tools",
    "website_tools",
    "whatsapp_tools",
    "wikipedia_tools",
    "x_tools",
    "yfinance_tools",
]


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
