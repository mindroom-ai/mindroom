"""Custom API tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes_sync
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
# Bodies are read undecoded and capped, so neither a large nor a compressed response can exhaust memory.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_REDIRECTS = 10


def _keeps_credentials(base: httpx.URL, url: httpx.URL) -> bool:
    """Mirror HTTPX's Authorization rule: the base_url origin, or its direct upgrade from http to https."""
    if url.host != base.host:
        return False
    if (url.scheme, url.port) == (base.scheme, base.port):
        return True
    return (base.scheme, base.port, url.scheme, url.port) == ("http", None, "https", None)


def _credential_header_guard(base_url: str, header_names: frozenset[str]) -> Callable[[httpx.Request], None]:
    """Return a request hook that strips credential headers from every hop outside the base_url origin."""
    base = httpx.URL(base_url)

    def strip_credentials_off_origin(request: httpx.Request) -> None:
        if not _keeps_credentials(base, request.url):
            for name in header_names:
                request.headers.pop(name, None)

    return strip_credentials_off_origin


def _read_final_response(client: httpx.Client, response: httpx.Response) -> str:
    """Follow redirects without reading their bodies, then report the final response within the byte cap."""
    for _ in range(_MAX_REDIRECTS):
        next_request = response.next_request
        if next_request is None:
            break
        response.close()
        response = client.send(next_request, stream=True)
    try:
        if response.next_request is not None:
            return json.dumps({"error": f"Request failed: more than {_MAX_REDIRECTS} redirects"}, indent=2)
        result: dict[str, object] = {
            "status_code": response.status_code,
            "headers": cast("dict[str, str]", redact_sensitive_data(dict(response.headers))),
        }
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding != "identity":
            result["error"] = f"Response used Content-Encoding {encoding} although identity was requested"
            return json.dumps(result, indent=2)
        try:
            body = collect_bounded_bytes_sync(response.iter_raw(), max_bytes=_MAX_RESPONSE_BYTES)
        except ByteLimitExceededError:
            result["error"] = f"Response body exceeds {_MAX_RESPONSE_BYTES} bytes"
            return json.dumps(result, indent=2)
    finally:
        response.close()
    try:
        result["data"] = json.loads(body)
    except ValueError:
        result["data"] = {"text": body.decode(response.encoding or "utf-8", errors="replace")}
    if not response.is_success:
        result["error"] = "Request failed"
    return json.dumps(result, indent=2)


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
            event_hooks = None
            request_headers = httpx.Headers(self._get_headers(headers))
            if has_credentials and self.base_url:
                # HTTPX already strips Authorization on these hops; configured headers need the same treatment.
                credential_headers = frozenset({"Authorization", *self.default_headers})
                event_hooks = {"request": [_credential_header_guard(self.base_url, credential_headers)]}
                # The URL alone chooses the virtual host that receives the credentials.
                request_headers.pop("Host", None)
            request_headers["Accept-Encoding"] = "identity"
            try:
                with httpx.Client(
                    transport=ServerFetchHTTPTransport(verify=self.verify_ssl),
                    event_hooks=event_hooks,
                ) as client:
                    request = client.build_request(
                        method=method,
                        url=url,
                        params=params,
                        data=data,
                        json=json_data,
                        headers=request_headers,
                        timeout=self.timeout,
                    )
                    return _read_final_response(client, client.send(request, auth=auth, stream=True))
            except httpx.RequestError as e:
                return json.dumps({"error": f"Request failed: {e}"}, indent=2)

    return MindRoomCustomApiTools
