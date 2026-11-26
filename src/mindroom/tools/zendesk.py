"""Zendesk Help Center search toolkit."""

from __future__ import annotations

import re
from typing import Any

import httpx

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import (
    UNSET,
    UnsetType,
    error_json,
    json_dumps,
    require_non_empty,
    resolve_toggle,
    safe_host_label,
)

_HTML_TAG_RE = re.compile(r"<.*?>")


class ZendeskTools(Toolkit):
    """Search Zendesk Help Center articles."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        company_name: str | None = None,
        enable_search_zendesk: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        self.username = username.strip() if isinstance(username, str) and username.strip() else None
        self.password = password.strip() if isinstance(password, str) and password.strip() else None
        self.company_name = (
            safe_host_label(company_name, "company_name")
            if isinstance(company_name, str) and company_name.strip()
            else None
        )
        self.timeout = timeout

        enable_all = resolve_toggle(all, default=False)
        tools: list[Any] = []
        if resolve_toggle(enable_search_zendesk, default=True, enable_all=enable_all):
            tools.append(self.search_zendesk)
        super().__init__(name="zendesk", tools=tools)

    def search_zendesk(self, search_string: str) -> str:
        """Search Zendesk Help Center articles matching `search_string`."""
        if not (self.username and self.password and self.company_name):
            return error_json("Zendesk username, password, and company_name are required.")
        try:
            query = require_non_empty(search_string, "search_string")
            url = f"https://{self.company_name}.zendesk.com/api/v2/help_center/articles/search.json"
            response = httpx.get(
                url,
                params={"query": query},
                auth=(self.username, self.password),
                timeout=self.timeout,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            articles = [_HTML_TAG_RE.sub("", str(article.get("body", ""))) for article in results]
            return json_dumps(articles)
        except (httpx.HTTPError, ValueError) as exc:
            return error_json(f"API request failed: {exc}")


@register_tool_with_metadata(
    name="zendesk",
    file_access=ToolFileAccess.NONE,
    display_name="Zendesk",
    description="Customer support platform for searching help center articles",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiZendesk",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="username",
            env=("ZENDESK_USERNAME",),
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            env=("ZENDESK_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="company_name",
            env=("ZENDESK_COMPANY_NAME",),
            label="Company Name",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search_zendesk",
            label="Enable Search Zendesk",
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
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/project-management/#zendesk",
    function_names=("search_zendesk",),
)
def zendesk_tools() -> type[ZendeskTools]:
    """Return Zendesk tools for searching help center articles."""
    return ZendeskTools
