"""DuckDuckGo tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.custom_tools.ddgs_search import DuckDuckGoTools


def ddgs_config_fields(
    field_order: tuple[str, ...],
    *,
    extra: Mapping[str, ConfigField] | None = None,
) -> list[ConfigField]:
    """Return fresh DDGS config fields in the requested dashboard order."""
    fields: dict[str, ConfigField] = {
        "enable_search": bool_config_field("enable_search", "Enable Search"),
        "enable_news": bool_config_field("enable_news", "Enable News"),
        "modifier": ConfigField(name="modifier", label="Modifier", type="text", required=False, default=None),
        "fixed_max_results": ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
        ),
        "timelimit": ConfigField(name="timelimit", label="Time Limit", type="text", required=False, default=None),
        "region": ConfigField(name="region", label="Region", type="text", required=False, default=None),
        "proxy": ConfigField(name="proxy", label="Proxy", type="url", required=False, default=None),
        "timeout": ConfigField(name="timeout", label="Timeout", type="number", required=False, default=10),
        "verify_ssl": bool_config_field("verify_ssl", "Verify Ssl"),
    }
    if extra:
        fields.update(extra)
    return [fields[name] for name in field_order]


@register_tool_with_metadata(
    name="duckduckgo",
    file_access=ToolFileAccess.NONE,
    display_name="DuckDuckGo",
    description="Search engine for web search and news",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiDuckduckgo",
    icon_color="text-orange-500",  # DuckDuckGo orange
    config_fields=ddgs_config_fields(
        (
            "enable_search",
            "enable_news",
            "modifier",
            "fixed_max_results",
            "timelimit",
            "region",
            "backend",
            "proxy",
            "timeout",
            "verify_ssl",
        ),
        extra={"backend": ConfigField(name="backend", label="Backend", type="text", required=False, default=None)},
    ),
    dependencies=["ddgs"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#duckduckgo",
    function_names=("web_search", "search_news"),
)
def duckduckgo_tools() -> type[DuckDuckGoTools]:
    """Return DuckDuckGo tools for web search and news."""
    from mindroom.custom_tools.ddgs_search import DuckDuckGoTools

    return DuckDuckGoTools
