"""Wikipedia tool configuration."""

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
    from agno.tools.wikipedia import WikipediaTools


@register_tool_with_metadata(
    name="wikipedia",
    display_name="Wikipedia",
    description="Search and retrieve information from Wikipedia",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaWikipediaW",
    icon_color="text-gray-700",
    config_fields=[
        ConfigField(
            name="knowledge_base",
            label="Knowledge Base",
            type="text",
            required=False,
            default=None,
            description="Optional WikipediaKnowledgeBase instance for advanced usage (typically left empty)",
        ),
    ],
    dependencies=["wikipedia"],
    docs_url="https://docs.agno.com/tools/toolkits/search/wikipedia",
)
def wikipedia_tools() -> type[WikipediaTools]:
    """Return Wikipedia tools for searching and retrieving information."""
    from agno.tools.wikipedia import WikipediaTools

    return WikipediaTools
