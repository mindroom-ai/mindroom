"""PubMed tool configuration."""

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
    from agno.tools.pubmed import PubmedTools


@register_tool_with_metadata(
    name="pubmed",
    display_name="PubMed",
    description="Search and retrieve medical and life science literature from PubMed",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaStethoscope",
    icon_color="text-blue-600",  # Medical blue
    config_fields=[
        # Configuration parameters
        ConfigField(
            name="email",
            label="Email Address",
            type="text",
            required=False,
            default="your_email@example.com",
            placeholder="researcher@institution.edu",
            description="Email address for NCBI API identification (required by NCBI guidelines)",
        ),
        ConfigField(
            name="max_results",
            label="Maximum Results",
            type="number",
            required=False,
            default=None,
            placeholder="10",
            description="Default maximum number of search results to return (can be overridden per search)",
        ),
        ConfigField(
            name="results_expanded",
            label="Expanded Results",
            type="boolean",
            required=False,
            default=False,
            description="Return comprehensive article metadata including keywords, MeSH terms, and full abstracts",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/search/pubmed",
)
def pubmed_tools() -> type[PubmedTools]:
    """Return PubMed tools for medical research and literature search."""
    from agno.tools.pubmed import PubmedTools

    return PubmedTools
