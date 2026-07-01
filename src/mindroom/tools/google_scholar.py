"""Google Scholar tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_scholar import GoogleScholarTools


@register_tool_with_metadata(
    name="google_scholar",
    display_name="Google Scholar",
    description="Search academic publications on Google Scholar",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiGooglescholar",
    icon_color="text-blue-500",  # Google Scholar blue
    config_fields=[
        ConfigField(
            name="max_results",
            label="Max Results",
            type="number",
            required=False,
            default=5,
        ),
    ],
    dependencies=["scholarly"],
    docs_url="https://github.com/scholarly-python-package/scholarly",
    function_names=("search_google_scholar",),
)
def google_scholar_tools() -> type[GoogleScholarTools]:
    """Return Google Scholar tools for academic publication search."""
    from mindroom.custom_tools.google_scholar import GoogleScholarTools

    return GoogleScholarTools
