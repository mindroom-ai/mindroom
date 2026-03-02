"""Tools registry for all available Agno tools.

This module provides a centralized registry for all tools that can be used by agents.
Tools are registered by string name and can be instantiated dynamically when loading agents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

from . import delegate as _delegate_registration  # noqa: F401
from . import memory as _memory_registration  # noqa: F401
from . import self_config as _self_config_registration  # noqa: F401
from .agentql import agentql_tools
from .airflow import airflow_tools
from .apify import apify_tools
from .arxiv import arxiv_tools
from .aws_lambda import aws_lambda_tools
from .aws_ses import aws_ses_tools
from .baidusearch import baidusearch_tools
from .bitbucket import bitbucket_tools
from .brandfetch import brandfetch_tools
from .brightdata import brightdata_tools
from .browser import browser_tools
from .browserbase import browserbase_tools
from .cal_com import cal_com_tools
from .calculator import calculator_tools
from .cartesia import cartesia_tools
from .claude_agent import claude_agent_tools
from .clickup import clickup_tools
from .coding import coding_tools
from .composio import composio_tools
from .config_manager import config_manager_tools
from .confluence import confluence_tools
from .crawl4ai import crawl4ai_tools
from .csv import csv_tools
from .custom_api import custom_api_tools
from .dalle import dalle_tools
from .daytona import daytona_tools
from .desi_vocal import desi_vocal_tools
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
from .file_generation import file_generation_tools
from .financial_datasets_api import financial_datasets_api_tools
from .firecrawl import firecrawl_tools
from .gemini import gemini_tools
from .giphy import giphy_tools
from .github import github_tools
from .gmail import gmail_tools
from .google_bigquery import google_bigquery_tools
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
from .matrix_message import matrix_message_tools
from .mem0 import mem0_tools
from .modelslabs import modelslabs_tools
from .moviepy_video_tools import moviepy_video_tools
from .neo4j import neo4j_tools
from .newspaper4k import newspaper4k_tools
from .notion import notion_tools
from .openai import openai_tools
from .openbb import openbb_tools
from .openweather import openweather_tools
from .oxylabs import oxylabs_tools
from .pandas import pandas_tools
from .postgres import postgres_tools
from .pubmed import pubmed_tools
from .python import python_tools
from .reasoning import reasoning_tools
from .reddit import reddit_tools
from .redshift import redshift_tools
from .replicate import replicate_tools
from .resend import resend_tools
from .scheduler import scheduler_tools
from .scrapegraph import scrapegraph_tools
from .searxng import searxng_tools
from .serpapi import serpapi_tools
from .serper import serper_tools
from .shell import shell_tools
from .shopify import shopify_tools
from .slack import slack_tools
from .sleep import sleep_tools
from .spider import spider_tools
from .spotify import spotify_tools
from .sql import sql_tools
from .subagents import subagents_tools
from .tavily import tavily_tools
from .telegram import telegram_tools
from .todoist import todoist_tools
from .trafilatura import trafilatura_tools
from .trello import trello_tools
from .twilio import twilio_tools
from .unsplash import unsplash_tools
from .visualization import visualization_tools
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
