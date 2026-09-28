"""Custom API tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from mindroom.redaction import redact_sensitive_data
from mindroom.server_fetch_url import ServerFetchHTTPTransport, validate_server_fetch_url
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools.api import CustomApiTools

_CREDENTIALS_NEED_BASE_URL = (
    "custom_api sends its configured api_key, username and password, and headers only to base_url; "
    "set base_url to call this API with them, or remove them to call arbitrary URLs"
)
_CROSS_ORIGIN_REDIRECT = "Refusing to follow a redirect to another origin with configured credentials"


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Return the scheme, host, and non-default port that bound where credentials may go."""
    return url.scheme, url.host, url.port


def _credential_origin_guard(base_url: str) -> Callable[[httpx.Request], None]:
    """Return a request hook that refuses every hop outside the base_url origin before it is sent."""
    credential_origin = _origin(httpx.URL(base_url))

    def keep_credentials_on_origin(request: httpx.Request) -> None:
        if _origin(request.url) != credential_origin:
            raise httpx.RequestError(_CROSS_ORIGIN_REDIRECT, request=request)

    return keep_credentials_on_origin


@register_tool_with_metadata(
    name="custom_api",
    file_access=ToolFileAccess.NONE,
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
            auth = (self.username, self.password) if self.username and self.password else None
            has_credentials = bool(self.api_key or auth or self.default_headers)
            if has_credentials and not self.base_url:
                return json.dumps({"error": _CREDENTIALS_NEED_BASE_URL}, indent=2)
            url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}" if self.base_url else endpoint
            url = validate_server_fetch_url(url)
            event_hooks = (
                {"request": [_credential_origin_guard(self.base_url)]} if has_credentials and self.base_url else None
            )
            try:
                with httpx.Client(
                    transport=ServerFetchHTTPTransport(verify=self.verify_ssl),
                    follow_redirects=True,
                    event_hooks=event_hooks,
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
                    "headers": cast("dict[str, str]", redact_sensitive_data(dict(response.headers))),
                    "data": response_data,
                }
                if not response.is_success:
                    result["error"] = "Request failed"
                return json.dumps(result, indent=2)
            except httpx.RequestError as e:
                return json.dumps({"error": f"Request failed: {e}"}, indent=2)

    return MindRoomCustomApiTools
