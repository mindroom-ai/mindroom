"""ArXiv tool configuration."""

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
    from agno.tools.arxiv import ArxivTools


@register_tool_with_metadata(
    name="arxiv",
    display_name="ArXiv",
    description="Search and read academic papers from ArXiv",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaFileAlt",
    icon_color="text-red-600",  # ArXiv red
    config_fields=[
        ConfigField(
            name="enable_search_arxiv",
            label="Enable Search Arxiv",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_arxiv_papers",
            label="Enable Read Arxiv Papers",
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
        ConfigField(
            name="download_dir",
            label="Download Dir",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["arxiv", "pypdf"],
    docs_url="https://docs.agno.com/tools/toolkits/search/arxiv",
)
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools

    return ArxivTools
