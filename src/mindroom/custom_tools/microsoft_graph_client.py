"""Microsoft Graph REST access for requester-scoped Microsoft 365 tools.

Every error raised here is safe to hand to a model: it carries a short code, a fixed
message, and non-sensitive details, never a request URL, header, token, or raw response body.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import httpx

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

GRAPH_ORIGIN = "https://graph.microsoft.com"
_GRAPH_ROOT = f"{GRAPH_ORIGIN}/v1.0"
_REQUEST_TIMEOUT_SECONDS = 20.0
_REQUEST_DEADLINE_SECONDS = 60.0
_UPLOAD_DEADLINE_SECONDS = 180.0
_MAX_JSON_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_SHARE_URL_CHARS = 2048
_MAX_ERROR_MESSAGE_CHARS = 300
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_ID_PART_PATTERN = re.compile(r"[A-Za-z0-9!_.-]{1,256}")
_GRAPH_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_STATUS_ERROR_CODES = {
    400: "invalid_request",
    403: "permission_denied",
    404: "not_found",
    409: "conflict",
    412: "conflict",
    413: "request_too_large",
    423: "locked",
    429: "rate_limited",
}


class GraphError(Exception):
    """A failure described only by a code, a fixed message, and non-sensitive details."""

    def __init__(self, *, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class GraphAccessRejectedError(GraphError):
    """Microsoft Graph rejected the access token, so the requester must reconnect."""


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """One OneDrive or SharePoint file, identified by its Graph drive and item IDs."""

    drive_id: str
    item_id: str

    def __post_init__(self) -> None:
        """Reject IDs whose characters could change a Graph request path."""
        for field_name, value in (("drive ID", self.drive_id), ("item ID", self.item_id)):
            if not _ID_PART_PATTERN.fullmatch(value):
                msg = f"The document's {field_name} contains unsupported characters."
                raise GraphError(code="invalid_document_id", message=msg)

    @classmethod
    def parse(cls, value: object) -> DocumentRef:
        """Return the reference named by a document ID of the form ``<drive id>:<item id>``."""
        drive_id, separator, item_id = value.partition(":") if isinstance(value, str) else ("", "", "")
        if not separator:
            msg = "document_id must be the value returned by connect_office_document, such as b!abc:01XYZ."
            raise GraphError(code="invalid_document_id", message=msg)
        return cls(drive_id=drive_id, item_id=item_id)

    @property
    def document_id(self) -> str:
        """Return the stable ID agents pass back to later calls."""
        return f"{self.drive_id}:{self.item_id}"

    def path(self, *segments: str) -> str:
        """Return this item's Graph path, with further raw path text appended as given."""
        return "/".join((graph_path("drives", self.drive_id, "items", self.item_id), *segments))


def graph_path(*segments: str) -> str:
    """Join path segments, percent-encoding each so no segment can add a separator or query."""
    return "/" + "/".join(quote(segment, safe="!") for segment in segments)


def share_id(url: object) -> str:
    """Encode a OneDrive or SharePoint link as a Graph share ID, rejecting anything but a plain HTTPS URL."""
    text = url.strip() if isinstance(url, str) else ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        parts = None
        port = None
    if (
        parts is None
        or not text
        or len(text) > _MAX_SHARE_URL_CHARS
        or parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or any(not char.isprintable() or char.isspace() for char in text)
    ):
        msg = "url must be an https:// OneDrive or SharePoint link without credentials."
        raise GraphError(code="invalid_url", message=msg)
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _new_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Build one short-lived client; redirects are never followed, so the bearer never leaves Graph."""
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False, transport=transport)


def _scrubbed(text: str) -> str:
    printable = "".join(char if char.isprintable() else " " for char in text)
    return _URL_PATTERN.sub("<url>", printable).strip()[:_MAX_ERROR_MESSAGE_CHARS]


def _graph_error_fields(content: bytes | None) -> dict[str, object]:
    """Return Graph's error code and scrubbed message from a failed response, without the raw body."""
    try:
        payload = json.loads(content) if content else None
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {}
    fields: dict[str, object] = {}
    code = error.get("code")
    if isinstance(code, str) and _GRAPH_CODE_PATTERN.fullmatch(code):
        fields["graph_code"] = code
    message = error.get("message")
    if isinstance(message, str) and (scrubbed := _scrubbed(message)):
        fields["graph_message"] = scrubbed
    return fields


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    value = headers.get("retry-after", "").strip()
    return int(value) if value.isdecimal() and len(value) <= 6 else None


def _status_error(status_code: int, content: bytes | None, headers: httpx.Headers) -> GraphError:
    fields = _graph_error_fields(content)
    if status_code == 401:
        return GraphAccessRejectedError(
            code="access_rejected",
            message="Microsoft 365 rejected the connected account's authorization.",
            status_code=status_code,
            **fields,
        )
    if status_code in {429, 503} and (retry_after := _retry_after_seconds(headers)) is not None:
        fields["retry_after_seconds"] = retry_after
    code = _STATUS_ERROR_CODES.get(status_code, "graph_unavailable" if status_code >= 500 else "graph_error")
    return GraphError(code=code, message="Microsoft Graph rejected the request.", status_code=status_code, **fields)


async def _bounded_body(response: httpx.Response) -> bytes | None:
    """Read a response body, or return None as soon as it is known to exceed the JSON bound."""
    declared = response.headers.get("content-length", "").strip()
    if declared.isdecimal() and int(declared) > _MAX_JSON_RESPONSE_BYTES:
        return None
    try:
        return await collect_bounded_bytes(response.aiter_bytes(), max_bytes=_MAX_JSON_RESPONSE_BYTES)
    except ByteLimitExceededError:
        return None


def _decoded_json(status_code: int, content: bytes | None, headers: httpx.Headers) -> object:
    if 300 <= status_code < 400:
        raise GraphError(
            code="redirect_rejected",
            message="Microsoft Graph answered with a redirect, which is not followed.",
            status_code=status_code,
        )
    if not httpx.codes.is_success(status_code):
        raise _status_error(status_code, content, headers)
    if content is None:
        raise GraphError(
            code="response_too_large",
            message=f"Microsoft Graph returned more than {_MAX_JSON_RESPONSE_BYTES} bytes. Narrow the request.",
        )
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        raise GraphError(
            code="invalid_response",
            message="Microsoft Graph returned a response that is not JSON.",
        ) from None


async def _send(
    method: str,
    path: str,
    access_token: str,
    *,
    params: Mapping[str, str] | None,
    json_body: object,
    content: bytes | None,
    deadline_seconds: float,
) -> object:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        async with (
            asyncio.timeout(deadline_seconds),
            _new_http_client() as client,
            client.stream(
                method,
                f"{_GRAPH_ROOT}{path}",
                headers=headers,
                params=dict(params) if params else None,
                json=json_body,
                content=content,
            ) as response,
        ):
            status_code = response.status_code
            body = await _bounded_body(response)
            response_headers = response.headers
    except TimeoutError:
        raise GraphError(
            code="request_timeout",
            message=f"The Microsoft Graph request did not finish within {deadline_seconds:.0f} seconds.",
        ) from None
    except httpx.HTTPError as exc:
        # httpx messages can include the request URL, so report only the failure type.
        raise GraphError(
            code="request_failed",
            message=f"The Microsoft Graph request failed ({type(exc).__name__}).",
        ) from None
    return _decoded_json(status_code, body, response_headers)


async def graph_json(
    access_token: str,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    json_body: object = None,
) -> object:
    """Send one bearer request to a Graph v1.0 path and return its decoded JSON."""
    return await _send(
        method,
        path,
        access_token,
        params=params,
        json_body=json_body,
        content=None,
        deadline_seconds=_REQUEST_DEADLINE_SECONDS,
    )


async def graph_upload(
    access_token: str,
    path: str,
    content: bytes,
    *,
    params: Mapping[str, str] | None = None,
) -> object:
    """PUT one file body to a Graph v1.0 content path and return the resulting driveItem."""
    return await _send(
        "PUT",
        path,
        access_token,
        params=params,
        json_body=None,
        content=content,
        deadline_seconds=_UPLOAD_DEADLINE_SECONDS,
    )
