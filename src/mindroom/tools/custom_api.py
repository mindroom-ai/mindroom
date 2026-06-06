"""Custom API tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_redirect_url, validate_server_fetch_url
from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.api import CustomApiTools

_MAX_CUSTOM_API_REDIRECTS = 10


def _custom_api_request_url(base_url: str | None, endpoint: str) -> str:
    """Return the URL shape Agno would request for a CustomApiTools call."""
    if base_url:
        return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    return endpoint


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
    import requests
    from agno.tools.api import CustomApiTools
    from agno.utils.log import log_debug, logger

    class MindRoomCustomApiTools(CustomApiTools):
        """CustomApiTools variant that validates server-side fetch targets."""

        def _validated_response(
            self,
            *,
            method: str,
            url: str,
            params: dict[str, Any] | None,
            data: dict[str, Any] | None,
            headers: dict[str, str] | None,
            json_data: dict[str, Any] | None,
        ) -> requests.Response:
            request_url = validate_server_fetch_url(url)
            for _redirect_count in range(_MAX_CUSTOM_API_REDIRECTS + 1):
                response = requests.request(
                    method=method,
                    url=request_url,
                    params=params,
                    data=data,
                    json=json_data,
                    headers=self._get_headers(headers),
                    auth=self._get_auth(),
                    verify=self.verify_ssl,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if not response.is_redirect:
                    return response
                request_url = validate_server_fetch_redirect_url(request_url, response.headers.get("Location"))
            msg = "Too many redirects while making Custom API request"
            raise requests.exceptions.TooManyRedirects(msg)

        def make_request(
            self,
            endpoint: str,
            method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = "GET",
            params: dict[str, Any] | None = None,
            data: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            json_data: dict[str, Any] | None = None,
        ) -> str:
            """Make a validated HTTP request to the API."""
            try:
                url = _custom_api_request_url(self.base_url, endpoint)
                log_debug(f"Making {method} request to {url}")
                response = self._validated_response(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    headers=headers,
                    json_data=json_data,
                )

                try:
                    response_data = response.json()
                except json.JSONDecodeError:
                    response_data = {"text": response.text}

                result: dict[str, Any] = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "data": response_data,
                }

                if not response.ok:
                    logger.error(f"Request failed with status {response.status_code}: {response.text}")
                    result["error"] = "Request failed"

                return json.dumps(result, indent=2)
            except ServerFetchUrlError:
                raise
            except requests.exceptions.RequestException as e:
                error_message = f"Request failed: {e}"
                logger.error(error_message)
                return json.dumps({"error": error_message}, indent=2)
            except Exception as e:
                error_message = f"Unexpected error: {e}"
                logger.error(error_message)
                return json.dumps({"error": error_message}, indent=2)

    return MindRoomCustomApiTools
