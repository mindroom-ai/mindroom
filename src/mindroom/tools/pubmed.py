"""PubMed tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.pubmed import PubmedTools


@register_tool_with_metadata(
    name="pubmed",
    display_name="PubMed",
    description="Search and retrieve medical and life science literature from PubMed",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiPubmed",
    icon_color="text-blue-600",  # Medical blue
    config_fields=[
        ConfigField(
            name="email",
            label="Email",
            type="text",
            required=False,
            default="your_email@example.com",
        ),
        ConfigField(
            name="max_results",
            label="Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="results_expanded",
            label="Results Expanded",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_search_pubmed",
            label="Enable Search Pubmed",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/search/pubmed",
)
def pubmed_tools() -> type[PubmedTools]:
    """Return PubMed tools for medical research and literature search."""
    from agno.tools.pubmed import PubmedTools

    return PubmedTools
