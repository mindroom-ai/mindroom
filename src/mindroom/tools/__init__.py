"""Built-in tool registrations.

Importing this package registers every built-in tool: it imports each tool's decorated factory,
or the module itself for a tool registered as metadata only, and registers three tools below.
Factories are imported by name, not through their modules, because privata counts only imports
from other modules as uses and would otherwise report those factories as private.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools import (
    agent_manager,
    compact_context,
    contacts,
    delegate,
    dynamic_tools,
    dynamic_workflow,
    knowledge_graph,
    memory,
    report_publishing,
    self_config,
    skill_manage,
)
from mindroom.tools.agentql import agentql_tools
from mindroom.tools.airflow import airflow_tools
from mindroom.tools.apify import apify_tools
from mindroom.tools.approved_egress import approved_egress_tools
from mindroom.tools.arxiv import arxiv_tools
from mindroom.tools.atlassian import atlassian_tools
from mindroom.tools.attachments import attachments_tools
from mindroom.tools.aws_lambda import aws_lambda_tools
from mindroom.tools.aws_ses import aws_ses_tools
from mindroom.tools.baidusearch import baidusearch_tools
from mindroom.tools.bitbucket import bitbucket_tools
from mindroom.tools.brandfetch import brandfetch_tools
from mindroom.tools.brightdata import brightdata_tools
from mindroom.tools.browser import browser_tools
from mindroom.tools.browser_mcp import browser_mcp_tools
from mindroom.tools.browserbase import browserbase_tools
from mindroom.tools.cal_com import cal_com_tools
from mindroom.tools.calculator import calculator_tools
from mindroom.tools.callback_manager import callback_manager_tools
from mindroom.tools.cartesia import cartesia_tools
from mindroom.tools.chat_ui import chat_ui_tools
from mindroom.tools.claude_agent import claude_agent_tools
from mindroom.tools.clickup import clickup_tools
from mindroom.tools.coding import coding_tools
from mindroom.tools.composio import composio_tools
from mindroom.tools.config_manager import config_manager_tools
from mindroom.tools.confluence import confluence_tools
from mindroom.tools.crawl4ai import crawl4ai_tools
from mindroom.tools.csv import csv_tools
from mindroom.tools.custom_api import custom_api_tools
from mindroom.tools.daytona import daytona_tools
from mindroom.tools.desktop import desktop_tools
from mindroom.tools.discord import discord_tools
from mindroom.tools.docker import docker_tools
from mindroom.tools.duckdb import duckdb_tools
from mindroom.tools.duckduckgo import duckduckgo_tools
from mindroom.tools.e2b import e2b_tools
from mindroom.tools.eleven_labs import eleven_labs_tools
from mindroom.tools.email import email_tools
from mindroom.tools.exa import exa_tools
from mindroom.tools.external_trigger_manager import external_trigger_manager_tools
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
from mindroom.tools.google_docs import google_docs_tools
from mindroom.tools.google_drive import google_drive_tools
from mindroom.tools.google_maps import google_maps_tools
from mindroom.tools.google_scholar import google_scholar_tools
from mindroom.tools.google_sheets import google_sheets_tools
from mindroom.tools.google_tasks import google_tasks_tools
from mindroom.tools.googlesearch import googlesearch_tools
from mindroom.tools.groq import groq_tools
from mindroom.tools.hackernews import hackernews_tools
from mindroom.tools.invite_router import invite_router_tools
from mindroom.tools.jina import jina_tools
from mindroom.tools.jira import jira_tools
from mindroom.tools.linear import linear_tools
from mindroom.tools.linkup import linkup_tools
from mindroom.tools.lumalabs import lumalabs_tools
from mindroom.tools.matrix_api import matrix_api_tools
from mindroom.tools.matrix_message import matrix_message_tools
from mindroom.tools.matrix_room import matrix_room_tools
from mindroom.tools.matrix_voice_message import matrix_voice_message_tools
from mindroom.tools.mem0 import mem0_tools
from mindroom.tools.microsoft_files import microsoft_files_tools
from mindroom.tools.microsoft_meetings import microsoft_meetings_tools
from mindroom.tools.microsoft_onenote import microsoft_onenote_tools
from mindroom.tools.microsoft_people import microsoft_people_tools
from mindroom.tools.microsoft_sharepoint import microsoft_sharepoint_tools
from mindroom.tools.microsoft_teams import microsoft_teams_tools
from mindroom.tools.microsoft_todo import microsoft_todo_tools
from mindroom.tools.modelslabs import modelslabs_tools
from mindroom.tools.moviepy_video_tools import moviepy_video_tools
from mindroom.tools.neo4j import neo4j_tools
from mindroom.tools.newspaper4k import newspaper4k_tools
from mindroom.tools.notion import notion_tools
from mindroom.tools.oauth_connections import oauth_connections_tools
from mindroom.tools.openai import openai_tools
from mindroom.tools.openbb import openbb_tools
from mindroom.tools.openweather import openweather_tools
from mindroom.tools.outlook_calendar import outlook_calendar_tools
from mindroom.tools.outlook_mail import outlook_mail_tools
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
from mindroom.tools.script import script_tools
from mindroom.tools.searxng import searxng_tools
from mindroom.tools.serpapi import serpapi_tools
from mindroom.tools.serper import serper_tools
from mindroom.tools.shared_directories import shared_directories_tools
from mindroom.tools.shell import shell_tools
from mindroom.tools.shopify import shopify_tools
from mindroom.tools.slack import slack_tools
from mindroom.tools.sleep import sleep_tools
from mindroom.tools.spider import spider_tools
from mindroom.tools.spotify import spotify_tools
from mindroom.tools.sql import sql_tools
from mindroom.tools.stop_reply import stop_reply_tools
from mindroom.tools.tavily import tavily_tools
from mindroom.tools.telegram import telegram_tools
from mindroom.tools.thread_model import thread_model_tools
from mindroom.tools.thread_move import thread_move_tools
from mindroom.tools.thread_resolution import thread_resolution_tools
from mindroom.tools.thread_summary import register_thread_summary_tools
from mindroom.tools.thread_tags import thread_tags_tools
from mindroom.tools.todo import todo_tools
from mindroom.tools.todoist import todoist_tools
from mindroom.tools.trafilatura import trafilatura_tools
from mindroom.tools.trello import trello_tools
from mindroom.tools.twilio import twilio_tools
from mindroom.tools.unsplash import unsplash_tools
from mindroom.tools.update_awareness import update_awareness_tools
from mindroom.tools.usage_stats import usage_stats_tools
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
    from mindroom.tool_system.authoring import Toolkit


# These three register here rather than in their own modules: a saved background job records its factory's
# module and name, and moving a factory makes that tool's jobs saved before an upgrade unavailable after it.
@register_tool_with_metadata(
    name="openclaw_compat",
    file_access=ToolFileAccess.NONE,
    display_name="OpenClaw Compat",
    description="Convenience bundle that implies shell, coding, browser, and other common tools",
    docs_url="https://docs.mindroom.chat/openclaw/#openclaw_compat",
    category=ToolCategory.DEVELOPMENT,
    icon="Workflow",
    icon_color="text-orange-500",
    helper_text="Implies: shell, coding, duckduckgo, website, browser, scheduler, matrix_message, attachments, matrix_room.",
)
def _openclaw_compat_tools() -> type[Toolkit]:
    """Return an empty toolkit — the real tools are loaded via tool preset expansion."""
    from mindroom.tool_system.authoring import Toolkit

    return Toolkit


@register_tool_with_metadata(
    name="homeassistant",
    file_access=ToolFileAccess.NONE,
    display_name="Home Assistant",
    description="Control and monitor smart home devices",
    category=ToolCategory.SMART_HOME,
    icon="Home",
    icon_color="text-blue-500",
    dependencies=["httpx"],
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    requires_primary_runtime=True,
    managed_init_args=(
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    ),
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
        ConfigField(
            name="HOMEASSISTANT_ALLOW_PRIVATE_URL",
            label="Allow Private Home Assistant URL",
            type="boolean",
            required=False,
            default=False,
            description="Allow a trusted self-hosted Home Assistant URL on a private, local, or loopback network.",
        ),
    ],
    docs_url="https://docs.mindroom.chat/tools/location-commerce-and-home/#homeassistant",
    function_names=(
        "activate_scene",
        "call_service",
        "get_entity_state",
        "list_entities",
        "set_brightness",
        "set_color",
        "set_temperature",
        "toggle",
        "trigger_automation",
        "turn_off",
        "turn_on",
    ),
)
def _homeassistant_tools() -> type[Toolkit]:
    """Return Home Assistant tools for smart home control."""
    from mindroom.custom_tools.homeassistant import HomeAssistantTools

    return HomeAssistantTools


@register_tool_with_metadata(
    name="egress_credentials",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Egress Credentials",
    description=(
        "Opt this agent's worker-routed shell and python calls into Agent Keys and see which keys and accounts it can use"
    ),
    docs_url="https://docs.mindroom.chat/deployment/egress-broker/#agent-tool",
    category=ToolCategory.INTEGRATIONS,
    icon="KeyRound",
    icon_color="text-amber-600",
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    config_fields=[],
    function_names=("list_egress_credentials",),
)
def _egress_credentials_tools() -> type[Toolkit]:
    """Return the egress broker credential status tool."""
    from mindroom.custom_tools.egress_credentials import EgressCredentialsTools

    return EgressCredentialsTools
