"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata
from mindroom.tools import delegate as _delegate_registration  # noqa: F401
from mindroom.tools import memory as _memory_registration  # noqa: F401
from mindroom.tools import self_config as _self_config_registration  # noqa: F401
from mindroom.tools.agentql import agentql_tools
from mindroom.tools.airflow import airflow_tools
from mindroom.tools.apify import apify_tools
from mindroom.tools.arxiv import arxiv_tools
from mindroom.tools.attachments import attachments_tools
from mindroom.tools.aws_lambda import aws_lambda_tools
from mindroom.tools.aws_ses import aws_ses_tools
from mindroom.tools.baidusearch import baidusearch_tools
from mindroom.tools.bitbucket import bitbucket_tools
from mindroom.tools.brandfetch import brandfetch_tools
from mindroom.tools.brightdata import brightdata_tools
from mindroom.tools.browser import browser_tools
from mindroom.tools.browserbase import browserbase_tools
from mindroom.tools.cal_com import cal_com_tools
from mindroom.tools.calculator import calculator_tools
from mindroom.tools.cartesia import cartesia_tools
from mindroom.tools.claude_agent import claude_agent_tools
from mindroom.tools.clickup import clickup_tools
from mindroom.tools.coding import coding_tools
from mindroom.tools.composio import composio_tools
from mindroom.tools.config_manager import config_manager_tools
from mindroom.tools.confluence import confluence_tools
from mindroom.tools.crawl4ai import crawl4ai_tools
from mindroom.tools.csv import csv_tools
from mindroom.tools.custom_api import custom_api_tools
from mindroom.tools.dalle import dalle_tools
from mindroom.tools.daytona import daytona_tools
from mindroom.tools.desi_vocal import desi_vocal_tools
from mindroom.tools.discord import discord_tools
from mindroom.tools.docker import docker_tools
from mindroom.tools.duckdb import duckdb_tools
from mindroom.tools.duckduckgo import duckduckgo_tools
from mindroom.tools.e2b import e2b_tools
from mindroom.tools.eleven_labs import eleven_labs_tools
from mindroom.tools.email import email_tools
from mindroom.tools.exa import exa_tools
from mindroom.tools.fal import fal_tools
from mindroom.tools.file import file_tools
from mindroom.tools.file_generation import file_generation_tools
from mindroom.tools.financial_datasets_api import financial_datasets_api_tools
from mindroom.tools.firecrawl import firecrawl_tools
from mindroom.tools.gemini import gemini_tools
from mindroom.tools.giphy import giphy_tools
from mindroom.tools.github import github_tools
from mindroom.tools.gmail import gmail_tools
from mindroom.tools.google_bigquery import google_bigquery_tools
from mindroom.tools.google_calendar import google_calendar_tools
from mindroom.tools.google_maps import google_maps_tools
from mindroom.tools.google_sheets import google_sheets_tools
from mindroom.tools.googlesearch import googlesearch_tools
from mindroom.tools.groq import groq_tools
from mindroom.tools.hackernews import hackernews_tools
from mindroom.tools.jina import jina_tools
from mindroom.tools.jira import jira_tools
from mindroom.tools.linear import linear_tools
from mindroom.tools.linkup import linkup_tools
from mindroom.tools.lumalabs import lumalabs_tools
from mindroom.tools.matrix_message import matrix_message_tools
from mindroom.tools.mem0 import mem0_tools
from mindroom.tools.modelslabs import modelslabs_tools
from mindroom.tools.moviepy_video_tools import moviepy_video_tools
from mindroom.tools.neo4j import neo4j_tools
from mindroom.tools.newspaper4k import newspaper4k_tools
from mindroom.tools.notion import notion_tools
from mindroom.tools.openai import openai_tools
from mindroom.tools.openbb import openbb_tools
from mindroom.tools.openweather import openweather_tools
from mindroom.tools.oxylabs import oxylabs_tools
from mindroom.tools.pandas import pandas_tools
from mindroom.tools.postgres import postgres_tools
from mindroom.tools.pubmed import pubmed_tools
from mindroom.tools.python import python_tools
from mindroom.tools.reasoning import reasoning_tools
from mindroom.tools.reddit import reddit_tools
from mindroom.tools.redshift import redshift_tools
from mindroom.tools.replicate import replicate_tools
from mindroom.tools.resend import resend_tools
from mindroom.tools.scheduler import scheduler_tools
from mindroom.tools.scrapegraph import scrapegraph_tools
from mindroom.tools.searxng import searxng_tools
from mindroom.tools.serpapi import serpapi_tools
from mindroom.tools.serper import serper_tools
from mindroom.tools.shell import shell_tools
from mindroom.tools.shopify import shopify_tools
from mindroom.tools.slack import slack_tools
from mindroom.tools.sleep import sleep_tools
from mindroom.tools.spider import spider_tools
from mindroom.tools.spotify import spotify_tools
from mindroom.tools.sql import sql_tools
from mindroom.tools.subagents import subagents_tools
from mindroom.tools.tavily import tavily_tools
from mindroom.tools.telegram import telegram_tools
from mindroom.tools.todoist import todoist_tools
from mindroom.tools.trafilatura import trafilatura_tools
from mindroom.tools.trello import trello_tools
from mindroom.tools.twilio import twilio_tools
from mindroom.tools.unsplash import unsplash_tools
from mindroom.tools.visualization import visualization_tools
from mindroom.tools.web_browser_tools import web_browser_tools
from mindroom.tools.webex import webex_tools
from mindroom.tools.website import website_tools
from mindroom.tools.whatsapp import whatsapp_tools
from mindroom.tools.wikipedia import wikipedia_tools
from mindroom.tools.x import x_tools
from mindroom.tools.yfinance import yfinance_tools
from mindroom.tools.youtube import youtube_tools
from mindroom.tools.zendesk import zendesk_tools
from mindroom.tools.zep import zep_tools
from mindroom.tools.zoom import zoom_tools

if TYPE_CHECKING:
    from agno.tools import Toolkit


__all__ = [
    "agentql_tools",
    "airflow_tools",
    "apify_tools",
    "arxiv_tools",
    "attachments_tools",
    "aws_lambda_tools",
    "aws_ses_tools",
    "baidusearch_tools",
    "bitbucket_tools",
    "brandfetch_tools",
    "brightdata_tools",
    "browser_tools",
    "browserbase_tools",
    "cal_com_tools",
    "calculator_tools",
    "cartesia_tools",
    "claude_agent_tools",
    "clickup_tools",
    "coding_tools",
    "composio_tools",
    "config_manager_tools",
    "confluence_tools",
    "crawl4ai_tools",
    "csv_tools",
    "custom_api_tools",
    "dalle_tools",
    "daytona_tools",
    "desi_vocal_tools",
    "discord_tools",
    "docker_tools",
    "duckdb_tools",
    "duckduckgo_tools",
    "e2b_tools",
    "eleven_labs_tools",
    "email_tools",
    "exa_tools",
    "fal_tools",
    "file_generation_tools",
    "file_tools",
    "financial_datasets_api_tools",
    "firecrawl_tools",
    "gemini_tools",
    "giphy_tools",
    "github_tools",
    "gmail_tools",
    "google_bigquery_tools",
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
    "matrix_message_tools",
    "mem0_tools",
    "modelslabs_tools",
    "moviepy_video_tools",
    "neo4j_tools",
    "newspaper4k_tools",
    "notion_tools",
    "openai_tools",
    "openbb_tools",
    "openweather_tools",
    "oxylabs_tools",
    "pandas_tools",
    "postgres_tools",
    "pubmed_tools",
    "python_tools",
    "reasoning_tools",
    "reddit_tools",
    "redshift_tools",
    "replicate_tools",
    "resend_tools",
    "scheduler_tools",
    "scrapegraph_tools",
    "searxng_tools",
    "serpapi_tools",
    "serper_tools",
    "shell_tools",
    "shopify_tools",
    "slack_tools",
    "sleep_tools",
    "spider_tools",
    "spotify_tools",
    "sql_tools",
    "subagents_tools",
    "tavily_tools",
    "telegram_tools",
    "todoist_tools",
    "trafilatura_tools",
    "trello_tools",
    "twilio_tools",
    "unsplash_tools",
    "visualization_tools",
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
    name="openclaw_compat",
    display_name="OpenClaw Compat",
    description="Convenience bundle that implies shell, coding, browser, and other common tools",
    category=ToolCategory.DEVELOPMENT,
    icon="Workflow",
    icon_color="text-orange-500",
    helper_text="Implies: shell, coding, duckduckgo, website, browser, scheduler, subagents, matrix_message, attachments.",
)
def openclaw_compat_tools() -> type[Toolkit]:
    """Return an empty toolkit — the real tools are loaded via IMPLIED_TOOLS."""
    from agno.tools import Toolkit

    return Toolkit


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
def _homeassistant_tools() -> type[Toolkit]:
    """Return Home Assistant tools for smart home control."""
    from mindroom.custom_tools.homeassistant import HomeAssistantTools

    return HomeAssistantTools
