"""ArXiv tool configuration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.arxiv import ArxivTools


@register_tool_with_metadata(
    name="arxiv",
    display_name="ArXiv",
    description="Search and read academic papers from ArXiv",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiArxiv",
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
    function_names=("read_arxiv_papers", "search_arxiv_and_return_articles"),
)
def arxiv_tools() -> type[ArxivTools]:
    """Return ArXiv tools for academic paper research."""
    from agno.tools.arxiv import ArxivTools

    class MindRoomArxivTools(ArxivTools):
        """ArXiv toolkit accepting serialized authored download paths."""

        def __init__(
            self,
            enable_search_arxiv: bool = True,
            enable_read_arxiv_papers: bool = True,
            all: bool = False,  # noqa: A002
            download_dir: Path | str | None = None,
            **kwargs: object,
        ) -> None:
            resolved_download_dir = Path(download_dir) if isinstance(download_dir, str) else download_dir
            super().__init__(
                enable_search_arxiv=enable_search_arxiv,
                enable_read_arxiv_papers=enable_read_arxiv_papers,
                all=all,
                download_dir=resolved_download_dir,
                **kwargs,
            )

    return MindRoomArxivTools
