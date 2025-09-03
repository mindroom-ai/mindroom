"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

from .agentql import agentql_tools
from .airflow import airflow_tools
from .apify import apify_tools
from .arxiv import arxiv_tools
from .aws_lambda import aws_lambda_tools
from .aws_ses import aws_ses_tools
from .baidusearch import baidusearch_tools
from .brightdata import brightdata_tools
from .browserbase import browserbase_tools
from .cal_com import cal_com_tools
from .calculator import calculator_tools
from .cartesia import cartesia_tools
from .composio import composio_tools
from .config_manager import config_manager_tools
from .confluence import confluence_tools
from .crawl4ai import crawl4ai_tools
from .csv import csv_tools
from .custom_api import custom_api_tools
from .dalle import dalle_tools
from .daytona import daytona_tools
from .discord import discord_tools
from .docker import docker_tools
from .duckdb import duckdb_tools
from .duckduckgo import duckduckgo_tools
from .e2b import e2b_tools
from .eleven_labs import eleven_labs_tools
from .email import email_tools
from .exa import exa_tools
from .fal import fal_tools
from .file import file_tools
from .financial_datasets_api import financial_datasets_api_tools
from .firecrawl import firecrawl_tools
from .gemini import gemini_tools
from .giphy import giphy_tools
from .github import github_tools
from .gmail import gmail_tools
from .google_calendar import google_calendar_tools
from .google_maps import google_maps_tools
from .google_sheets import google_sheets_tools
from .googlesearch import googlesearch_tools
from .groq import groq_tools
from .hackernews import hackernews_tools
from .jina import jina_tools
from .jira import jira_tools
from .linear import linear_tools
from .linkup import linkup_tools
from .lumalabs import lumalabs_tools
from .mem0 import mem0_tools
from .modelslabs import modelslabs_tools
from .moviepy_video_tools import moviepy_video_tools
from .newspaper4k import newspaper4k_tools
from .openai import openai_tools
from .openweather import openweather_tools
from .oxylabs import oxylabs_tools
from .pandas import pandas_tools
from .pubmed import pubmed_tools
from .python import python_tools
from .reddit import reddit_tools
from .replicate import replicate_tools
from .resend import resend_tools
from .scrapegraph import scrapegraph_tools
from .searxng import searxng_tools
from .serpapi import serpapi_tools
from .serper import serper_tools
from .shell import shell_tools
from .slack import slack_tools
from .sleep import sleep_tools
from .spider import spider_tools
from .sql import sql_tools
from .tavily import tavily_tools
from .telegram import telegram_tools
from .todoist import todoist_tools
from .trello import trello_tools
from .twilio import twilio_tools
from .web_browser_tools import web_browser_tools
from .webex import webex_tools
from .website import website_tools
from .whatsapp import whatsapp_tools
from .wikipedia import wikipedia_tools
from .x import x_tools
from .yfinance import yfinance_tools
from .youtube import youtube_tools
from .zendesk import zendesk_tools
from .zep import zep_tools
from .zoom import zoom_tools

if TYPE_CHECKING:
    from agno.tools import Toolkit


__all__ = [
    "agentql_tools",
    "airflow_tools",
    "apify_tools",
    "arxiv_tools",
    "aws_lambda_tools",
    "aws_ses_tools",
    "baidusearch_tools",
    "brightdata_tools",
    "browserbase_tools",
    "cal_com_tools",
    "calculator_tools",
    "cartesia_tools",
    "composio_tools",
    "config_manager_tools",
    "confluence_tools",
    "crawl4ai_tools",
    "csv_tools",
    "custom_api_tools",
    "dalle_tools",
    "daytona_tools",
    "discord_tools",
    "docker_tools",
    "duckdb_tools",
    "duckduckgo_tools",
    "e2b_tools",
    "eleven_labs_tools",
    "email_tools",
    "exa_tools",
    "fal_tools",
    "file_tools",
    "financial_datasets_api_tools",
    "firecrawl_tools",
    "gemini_tools",
    "giphy_tools",
    "github_tools",
    "gmail_tools",
    "google_calendar_tools",
    "google_maps_tools",
    "google_sheets_tools",
    "googlesearch_tools",
    "groq_tools",
    "hackernews_tools",
    "jina_tools",
    "jira_tools",
    "linear_tools",
    "linkup_tools",
    "lumalabs_tools",
    "mem0_tools",
    "modelslabs_tools",
    "moviepy_video_tools",
    "newspaper4k_tools",
    "newspaper_tools",
    "openai_tools",
    "openweather_tools",
    "oxylabs_tools",
    "pandas_tools",
    "pubmed_tools",
    "python_tools",
    "reddit_tools",
    "replicate_tools",
    "resend_tools",
    "scrapegraph_tools",
    "searxng_tools",
    "serpapi_tools",
    "serper_tools",
    "shell_tools",
    "slack_tools",
    "sleep_tools",
    "spider_tools",
    "sql_tools",
    "tavily_tools",
    "telegram_tools",
    "todoist_tools",
    "trello_tools",
    "twilio_tools",
    "web_browser_tools",
    "webex_tools",
    "website_tools",
    "whatsapp_tools",
    "wikipedia_tools",
    "x_tools",
    "yfinance_tools",
    "youtube_tools",
    "zendesk_tools",
    "zep_tools",
    "zoom_tools",
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
    from mindroom.custom_tools.homeassistant import HomeAssistantTools

    return HomeAssistantTools


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
