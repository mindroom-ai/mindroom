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
        # Feature flags
        ConfigField(
            name="search_arxiv",
            label="Search ArXiv",
            type="boolean",
            required=False,
            default=True,
            description="Enable searching ArXiv for academic papers",
        ),
        ConfigField(
            name="read_arxiv_papers",
            label="Read ArXiv Papers",
            type="boolean",
            required=False,
            default=True,
            description="Enable downloading and reading ArXiv papers",
        ),
        # Configuration
        ConfigField(
            name="download_dir",
            label="Download Directory",
            type="text",
            required=False,
            default=None,
            placeholder="/path/to/downloads",
            description="Directory to download PDF files (defaults to arxiv_pdfs in tool directory)",
        ),
    ],
    dependencies=["arxiv", "pypdf"],
    docs_url="https://docs.agno.com/tools/toolkits/search/arxiv",
)
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools

    return ArxivTools
