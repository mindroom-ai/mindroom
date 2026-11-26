"""Notion page management toolkit."""

from __future__ import annotations

from typing import Any, Literal, cast

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    UnsetType,
    error_json,
    json_dumps,
    require_non_blank_text,
    require_non_empty,
    resolve_toggle,
    safe_path_segment,
)

_WriteAction = Literal["create_page", "update_page"]
_NOTION_API_VERSION = "2022-06-28"


class _NotionTools(Toolkit):
    """Create, update, and search pages in a Notion database."""

    def __init__(
        self,
        api_key: str | None = None,
        database_id: str | None = None,
        enable_create_page: bool | None | UnsetType = UNSET,
        enable_update_page: bool | None | UnsetType = UNSET,
        enable_search_pages: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
    ) -> None:
        self.api_key = require_non_empty(api_key, "Notion API key")
        raw_database_id = require_non_empty(database_id, "Notion database ID")
        self.database_id = safe_path_segment(raw_database_id, "database_id")

        from notion_client import Client

        self.client: Any = Client(auth=self.api_key)

        enable_all = resolve_toggle(all, default=False)
        self._allow_create = resolve_toggle(enable_create_page, default=True, enable_all=enable_all)
        self._allow_update = resolve_toggle(enable_update_page, default=True, enable_all=enable_all)
        self._allow_search = resolve_toggle(enable_search_pages, default=True, enable_all=enable_all)

        tools: list[Any] = []
        if self._allow_search:
            tools.append(self.notion_search)
        if self._allow_create or self._allow_update:
            tools.append(self.notion_write)
        super().__init__(name="notion", tools=tools)

    def notion_search(self, tag: str) -> str:
        """Search Notion database pages matching a `Tag` select value."""
        try:
            clean_tag = require_non_empty(tag, "tag")
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Notion-Version": _NOTION_API_VERSION,
                "Content-Type": "application/json",
            }
            response = httpx.post(
                f"https://api.notion.com/v1/databases/{self.database_id}/query",
                headers=headers,
                json={"filter": {"property": "Tag", "select": {"equals": clean_tag}}},
                timeout=30.0,
            )
            if response.status_code != 200:
                return json_dumps(
                    {
                        "success": False,
                        "error": f"API request failed with status {response.status_code}",
                        "message": response.text,
                    },
                )
            data = response.json()
            pages: list[dict[str, Any]] = []
            for page in data.get("results", []):
                if not isinstance(page, dict):
                    continue
                props = page.get("properties") if isinstance(page.get("properties"), dict) else {}
                title_items = props.get("Name", {}).get("title") if isinstance(props.get("Name"), dict) else None
                page_title = (
                    title_items[0].get("text", {}).get("content", "Untitled")
                    if isinstance(title_items, list) and title_items and isinstance(title_items[0], dict)
                    else "Untitled"
                )
                tag_select = props.get("Tag", {}).get("select") if isinstance(props.get("Tag"), dict) else None
                page_tag = tag_select.get("name") if isinstance(tag_select, dict) else None
                pages.append(
                    {
                        "page_id": page.get("id", ""),
                        "title": page_title,
                        "tag": page_tag,
                        "url": page.get("url", ""),
                    },
                )
            return json_dumps({"success": True, "count": len(pages), "pages": pages})
        except Exception as exc:
            return error_json(str(exc), success=False)

    def notion_write(
        self,
        action: _WriteAction,
        title: str | None = None,
        tag: str | None = None,
        content: str | None = None,
        page_id: str | None = None,
    ) -> str:
        """Create or append content to Notion pages.

        Actions:
        - `create_page`: Create a page in the database (`title`, `tag`, `content`).
        - `update_page`: Append a paragraph block to `page_id` (`page_id`, `content`).
        """
        try:
            if action == "create_page":
                if not self._allow_create:
                    return error_json("Creating Notion pages is disabled.", success=False)
                clean_title = require_non_empty(title, "title")
                clean_tag = require_non_empty(tag, "tag")
                body_text = require_non_blank_text(content, "content")
                new_page = cast(
                    "dict[str, Any]",
                    self.client.pages.create(
                        parent={"database_id": self.database_id},
                        properties={
                            "Name": {"title": [{"text": {"content": clean_title}}]},
                            "Tag": {"select": {"name": clean_tag}},
                        },
                        children=[
                            {
                                "object": "block",
                                "type": "paragraph",
                                "paragraph": {"rich_text": [{"type": "text", "text": {"content": body_text}}]},
                            },
                        ],
                    ),
                )
                return json_dumps(
                    {
                        "success": True,
                        "page_id": new_page["id"],
                        "url": new_page["url"],
                        "title": clean_title,
                        "tag": clean_tag,
                    },
                )

            if not self._allow_update:
                return error_json("Updating Notion pages is disabled.", success=False)
            safe_page = safe_path_segment(require_non_empty(page_id, "page_id"), "page_id")
            body_text = require_non_blank_text(content, "content")
            self.client.blocks.children.append(
                block_id=safe_page,
                children=[
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": body_text}}]},
                    },
                ],
            )
            return json_dumps({"success": True, "page_id": safe_page, "message": "Content added successfully"})
        except Exception as exc:
            return error_json(str(exc), success=False)


@register_tool_with_metadata(
    name="notion",
    file_access=ToolFileAccess.NONE,
    display_name="Notion",
    description="Create, update, and search pages in Notion databases",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiNotion",
    icon_color="text-gray-800",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("NOTION_API_KEY",),
            label="API Key",
            type="password",
            required=True,
            placeholder="Notion integration token",
            description="Internal integration token",
        ),
        ConfigField(
            name="database_id",
            env=("NOTION_DATABASE_ID",),
            label="Database ID",
            type="text",
            required=True,
            placeholder="Notion database ID",
            description="ID of the Notion database to work with",
        ),
        ConfigField(
            name="enable_create_page",
            label="Enable Create Page",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_update_page",
            label="Enable Update Page",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_pages",
            label="Enable Search Pages",
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
    dependencies=["notion-client"],
    docs_url="https://docs.mindroom.chat/tools/project-management/#notion",
    helper_text="Create an integration at [Notion Developers](https://www.notion.so/my-integrations) and share the database with it",
    function_names=("notion_search", "notion_write"),
    action_scoped_grants=("notion_write",),
)
def notion_tools() -> type[Toolkit]:
    """Return Notion tools for page management."""
    return _NotionTools
