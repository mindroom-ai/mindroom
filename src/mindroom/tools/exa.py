"""Exa tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.exa import ExaTools


@register_tool_with_metadata(
    name="exa",
    display_name="Exa",
    description="Advanced AI-powered web search engine for research and content discovery",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="exa_...",
            description="Exa API key for authentication (can also be set via EXA_API_KEY env var)",
        ),
        # Tool functionality flags
        ConfigField(
            name="search",
            label="Search",
            type="boolean",
            required=False,
            default=True,
            description="Enable web search functionality",
        ),
        ConfigField(
            name="get_contents",
            label="Get Contents",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving detailed content from URLs",
        ),
        ConfigField(
            name="find_similar",
            label="Find Similar",
            type="boolean",
            required=False,
            default=True,
            description="Enable finding similar pages to a given URL",
        ),
        ConfigField(
            name="answer",
            label="Answer",
            type="boolean",
            required=False,
            default=True,
            description="Enable LLM-powered question answering with search results",
        ),
        ConfigField(
            name="research",
            label="Research",
            type="boolean",
            required=False,
            default=False,
            description="Enable deep research functionality with structured output",
        ),
        # Content options
        ConfigField(
            name="text",
            label="Text",
            type="boolean",
            required=False,
            default=True,
            description="Retrieve text content from search results",
        ),
        ConfigField(
            name="text_length_limit",
            label="Text Length Limit",
            type="number",
            required=False,
            default=1000,
            description="Maximum length of text content per result",
        ),
        ConfigField(
            name="highlights",
            label="Highlights",
            type="boolean",
            required=False,
            default=True,
            description="Include highlighted snippets in results",
        ),
        ConfigField(
            name="summary",
            label="Summary",
            type="boolean",
            required=False,
            default=False,
            description="Include AI-generated summaries in results",
        ),
        # Search configuration
        ConfigField(
            name="num_results",
            label="Number of Results",
            type="number",
            required=False,
            placeholder="10",
            description="Default number of search results to return (overrides individual searches if set)",
        ),
        ConfigField(
            name="livecrawl",
            label="Live Crawl",
            type="text",
            required=False,
            default="always",
            placeholder="always",
            description="Live crawl setting for fresh content",
        ),
        # Date filtering
        ConfigField(
            name="start_crawl_date",
            label="Start Crawl Date",
            type="text",
            required=False,
            placeholder="2024-01-01",
            description="Include results crawled on/after this date (YYYY-MM-DD format)",
        ),
        ConfigField(
            name="end_crawl_date",
            label="End Crawl Date",
            type="text",
            required=False,
            placeholder="2024-12-31",
            description="Include results crawled on/before this date (YYYY-MM-DD format)",
        ),
        ConfigField(
            name="start_published_date",
            label="Start Published Date",
            type="text",
            required=False,
            placeholder="2024-01-01",
            description="Include results published on/after this date (YYYY-MM-DD format)",
        ),
        ConfigField(
            name="end_published_date",
            label="End Published Date",
            type="text",
            required=False,
            placeholder="2024-12-31",
            description="Include results published on/before this date (YYYY-MM-DD format)",
        ),
        # Search enhancement
        ConfigField(
            name="use_autoprompt",
            label="Use Autoprompt",
            type="boolean",
            required=False,
            description="Enable autoprompt features to improve query understanding",
        ),
        ConfigField(
            name="type",
            label="Content Type",
            type="text",
            required=False,
            placeholder="article",
            description="Specify content type filter (e.g., article, blog, video)",
        ),
        ConfigField(
            name="category",
            label="Category",
            type="text",
            required=False,
            placeholder="research paper",
            description='Filter results by category. Options: "company", "research paper", "news", "pdf", "github", "tweet", "personal site", "linkedin profile", "financial report"',
        ),
        ConfigField(
            name="include_domains",
            label="Include Domains",
            type="text",
            required=False,
            placeholder="example.com,google.com",
            description="Comma-separated list of domains to restrict results to",
        ),
        ConfigField(
            name="exclude_domains",
            label="Exclude Domains",
            type="text",
            required=False,
            placeholder="spam.com,ads.com",
            description="Comma-separated list of domains to exclude from results",
        ),
        # Model settings
        ConfigField(
            name="model",
            label="Search Model",
            type="text",
            required=False,
            placeholder="exa",
            description='The search model to use. Options: "exa" or "exa-pro"',
        ),
        ConfigField(
            name="research_model",
            label="Research Model",
            type="text",
            required=False,
            default="exa-research",
            placeholder="exa-research",
            description='Model for research functionality. Options: "exa-research" or "exa-research-pro"',
        ),
        # System settings
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            description="Maximum time in seconds to wait for API responses",
        ),
        ConfigField(
            name="show_results",
            label="Show Results",
            type="boolean",
            required=False,
            default=False,
            description="Log search results for debugging purposes",
        ),
    ],
    dependencies=["exa_py"],
    docs_url="https://docs.agno.com/tools/toolkits/search/exa",
)
def exa_tools() -> type[ExaTools]:
    """Return Exa tools for AI-powered web search and research."""
    from agno.tools.exa import ExaTools

    return ExaTools
