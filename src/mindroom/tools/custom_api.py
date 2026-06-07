"""Custom API tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.server_fetch_url import ServerFetchHTTPTransport, validate_server_fetch_url
from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools.api import CustomApiTools

_SENSITIVE_RESPONSE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "authentication-info",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "www-authenticate",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
        "x-xsrf-token",
    },
)
_SENSITIVE_RESPONSE_HEADER_MARKERS = (
    "api-key",
    "apikey",
    "secret",
    "access-token",
    "auth-token",
    "bearer-token",
    "csrf-token",
    "id-token",
    "refresh-token",
    "session-token",
    "xsrf-token",
)


def _sanitize_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return response headers safe for tool-visible output."""
    return {name: value for name, value in headers.items() if not _is_sensitive_response_header(name)}


def _is_sensitive_response_header(name: str) -> bool:
    normalized_name = name.strip().lower()
    return normalized_name in _SENSITIVE_RESPONSE_HEADER_NAMES or any(
        marker in normalized_name for marker in _SENSITIVE_RESPONSE_HEADER_MARKERS
    )


@register_tool_with_metadata(
    name="custom_api",
    display_name="Custom API",
    description="Make HTTP requests to any external API with customizable authentication and parameters",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Globe",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="headers",
            label="Headers",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify Ssl",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
        ConfigField(
            name="enable_make_request",
            label="Enable Make Request",
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
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/custom_api",
    function_names=("make_request",),
)
def custom_api_tools() -> type[CustomApiTools]:
    """Return Custom API tools for making HTTP requests to external APIs."""
    from agno.tools.api import CustomApiTools

    class MindRoomCustomApiTools(CustomApiTools):
        """Custom API toolkit with MindRoom server-fetch URL validation."""

        def make_request(
            self,
            endpoint: str,
            method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = "GET",
            params: dict[str, Any] | None = None,
            data: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            json_data: dict[str, Any] | None = None,
        ) -> str:
            """Make an HTTP request to a validated public HTTP(S) URL."""
            url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}" if self.base_url else endpoint
            url = validate_server_fetch_url(url)
            auth = (self.username, self.password) if self.username and self.password else None
            try:
                with httpx.Client(
                    transport=ServerFetchHTTPTransport(verify=self.verify_ssl),
                    follow_redirects=True,
                ) as client:
                    response = client.request(
                        method=method,
                        url=url,
                        params=params,
                        data=data,
                        json=json_data,
                        headers=self._get_headers(headers),
                        auth=auth,
                        timeout=self.timeout,
                    )

                try:
                    response_data: object = response.json()
                except ValueError:
                    response_data = {"text": response.text}

                result: dict[str, object] = {
                    "status_code": response.status_code,
                    "headers": _sanitize_response_headers(response.headers),
                    "data": response_data,
                }
                if not response.is_success:
                    result["error"] = "Request failed"
                return json.dumps(result, indent=2)
            except httpx.RequestError as e:
                return json.dumps({"error": f"Request failed: {e}"}, indent=2)

    return MindRoomCustomApiTools
