"""Linkup real-time web search toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
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
    from collections.abc import Callable

logger = get_logger(__name__)


class _LinkupTools(Toolkit):
    """Search the web using the Linkup API."""

    def __init__(
        self,
        api_key: str | None = None,
        depth: Literal["standard", "deep"] = "standard",
        output_type: Literal["sourcedAnswer", "searchResults"] = "searchResults",
        enable_web_search_with_linkup: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        from linkup import LinkupClient

        self.api_key = api_key
        if not self.api_key:
            logger.error("linkup_api_key_missing")

        self.linkup: Any = LinkupClient(api_key=self.api_key)
        self.depth = depth
        self.output_type = output_type

        tools: list[Callable[..., Any]] = []
        if all or enable_web_search_with_linkup:
            tools.append(self.web_search_with_linkup)

        super().__init__(name="linkup_tools", tools=tools)

    def web_search_with_linkup(
        self,
        query: str,
        depth: str | None = None,
        output_type: str | None = None,
    ) -> str:
        """Use this function to search the web for a given query.

        This function uses the Linkup API to provide realtime online information about the query.

        Args:
            query: Query to search for.
            depth: (deep|standard) Depth of the search. Defaults to 'standard'.
            output_type: (sourcedAnswer|searchResults) Type of output. Defaults to 'searchResults'.

        Returns:
            String of results related to the query.

        """
        try:
            response = self.linkup.search(
                query=query,
                depth=depth or self.depth,
                output_type=output_type or self.output_type,
            )
        except Exception as exc:
            return f"Error: {exc}"
        if hasattr(response, "model_dump_json"):
            return response.model_dump_json()
        if isinstance(response, (dict, list)):
            return json.dumps(response, default=str)
        return str(response)


@register_tool_with_metadata(
    name="linkup",
    file_access=ToolFileAccess.NONE,
    display_name="Linkup",
    description="Web search using Linkup API for real-time information",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("LINKUP_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="depth",
            label="Depth",
            type="text",
            required=False,
            default="standard",
        ),
        ConfigField(
            name="output_type",
            label="Output Type",
            type="text",
            required=False,
            default="searchResults",
        ),
        bool_config_field("enable_web_search_with_linkup", "Enable Web Search With Linkup"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["linkup-sdk"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#linkup",
    function_names=("web_search_with_linkup",),
)
def linkup_tools() -> type[Toolkit]:
    """Return Linkup tools for web search."""
    return _LinkupTools
