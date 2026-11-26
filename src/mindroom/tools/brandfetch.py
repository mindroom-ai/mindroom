"""Brandfetch brand assets and search toolkit."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

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
    safe_path_segment,
)

_LookupAction = Literal["by_identifier", "by_brand"]


def _validate_base_url(value: str) -> str:
    cleaned = require_non_empty(value, "base_url").rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = "Invalid Brandfetch base_url."
        raise ValueError(msg)
    return cleaned


class BrandfetchTools(Toolkit):
    """Retrieve brand assets by identifier or search brands by name via Brandfetch API v2."""

    def __init__(
        self,
        api_key: str | None = None,
        client_id: str | None = None,
        base_url: str = "https://api.brandfetch.io/v2",
        timeout: float | None = 20.0,
        enable_search_by_identifier: bool | None | UnsetType = UNSET,
        enable_search_by_brand: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
    ) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.client_id = client_id.strip() if isinstance(client_id, str) and client_id.strip() else None
        self.base_url = _validate_base_url(base_url)
        self.timeout = httpx.Timeout(timeout)
        self.brand_url = f"{self.base_url}/brands"
        self.search_url = f"{self.base_url}/search"

        enable_all = resolve_toggle(all, default=False)
        self._allow_identifier = resolve_toggle(enable_search_by_identifier, default=True, enable_all=enable_all)
        self._allow_brand = resolve_toggle(enable_search_by_brand, default=False, enable_all=enable_all)

        tools: list[Any] = []
        if self._allow_identifier or self._allow_brand:
            tools.append(self.brandfetch_lookup)
        super().__init__(name="brandfetch", tools=tools)

    def _status_error(self, status_code: int, *, action: _LookupAction, query: str) -> str:
        if status_code == 404:
            if action == "by_identifier":
                return error_json(f"Brand not found for identifier: {query}")
            return error_json(f"No brands found for name: {query}")
        if status_code == 401:
            return error_json("Invalid API key" if action == "by_identifier" else "Invalid client ID")
        if status_code == 429:
            return error_json("Rate limit exceeded")
        return error_json(f"API error: {status_code}")

    def brandfetch_lookup(
        self,
        query: str,
        action: _LookupAction = "by_identifier",
    ) -> str:
        """Retrieve Brandfetch brand assets or search brands by name.

        Actions:
        - `by_identifier`: Fetch brand assets for domain, brand ID, ISIN, or ticker `query` (requires `api_key`).
        - `by_brand`: Search brands by name `query` (requires `client_id` and `enable_search_by_brand`).
        """
        try:
            url, headers, params = self._request_spec(action, query)
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, headers=headers, params=params)
                response.raise_for_status()
                return json_dumps(response.json())
        except httpx.HTTPStatusError as exc:
            return self._status_error(exc.response.status_code, action=action, query=query)
        except (httpx.RequestError, ValueError) as exc:
            return error_json(str(exc))

    def _request_spec(
        self,
        action: _LookupAction,
        query: str,
    ) -> tuple[str, dict[str, str] | None, dict[str, str] | None]:
        safe_query = safe_path_segment(require_non_empty(query, "query"), "query")
        if action == "by_identifier":
            if not self._allow_identifier:
                msg = "Brandfetch search by identifier is disabled."
                raise ValueError(msg)
            if not self.api_key:
                msg = "API key is required for brand search by identifier."
                raise ValueError(msg)
            return f"{self.brand_url}/{safe_query}", {"Authorization": f"Bearer {self.api_key}"}, None
        if not self._allow_brand:
            msg = "Brandfetch search by brand name is disabled."
            raise ValueError(msg)
        if not self.client_id:
            msg = "Client ID is required for brand search by name."
            raise ValueError(msg)
        return f"{self.search_url}/{safe_query}", None, {"c": self.client_id}


@register_tool_with_metadata(
    name="brandfetch",
    file_access=ToolFileAccess.NONE,
    display_name="Brandfetch",
    description="Retrieve brand data including logos, colors, and fonts by domain or name",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaPalette",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("BRANDFETCH_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            description="Brandfetch API key for Brand API",
        ),
        ConfigField(
            name="client_id",
            env=("BRANDFETCH_CLIENT_ID",),
            label="Client ID",
            type="text",
            required=False,
            description="Brandfetch Client ID for Brand Search API",
        ),
        ConfigField(
            name="enable_search_by_identifier",
            label="Enable Search by Identifier",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_by_brand",
            label="Enable Search by Brand",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default="https://api.brandfetch.io/v2",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=20.0,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#brandfetch",
    helper_text="Get API keys from [Brandfetch Developers](https://developers.brandfetch.com/)",
    function_names=("brandfetch_lookup",),
    action_scoped_grants=("brandfetch_lookup",),
)
def brandfetch_tools() -> type[BrandfetchTools]:
    """Return Brandfetch tools for brand data retrieval."""
    return BrandfetchTools
