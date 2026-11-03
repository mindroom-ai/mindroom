"""Smartsheet sheets and rows through an API access token or the requester's OAuth connection.

Every error payload is safe to hand to a model: a short code, a fixed message, and non-sensitive details,
never a token, a request header, or a raw response body.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import httpx

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.logging_config import get_logger
from mindroom.oauth.client import active_oauth_credential_context
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError, oauth_connection_required_payload
from mindroom.oauth.service import OAUTH_ACCESS_REJECTED_REASON, current_oauth_access_token, oauth_connection_required
from mindroom.oauth.smartsheet import smartsheet_oauth_provider
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.authoring import Toolkit, tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.oauth.credential_lifecycle import OAuthCredentialContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_API_BASE_URL = "https://api.smartsheet.com/2.0"
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_PAGE_SIZE = 500
_MAX_ROW_IDS = 100
_MAX_ERROR_MESSAGE_CHARS = 300
_STATUS_ERROR_CODES = {
    400: "invalid_request",
    403: "permission_denied",
    404: "not_found",
    429: "rate_limited",
}
_API_KEY_REJECTED_MESSAGE = (
    "Smartsheet rejected the configured access token. Check the saved Smartsheet access token or "
    "SMARTSHEET_ACCESS_TOKEN; tokens from Smartsheet Regions Europe, Australia, or Gov do not work here."
)

type _Operation = Callable[[str], Awaitable[dict[str, object]]]


class _SmartsheetError(Exception):
    """A failure described only by a code, a fixed message, and non-sensitive details."""

    def __init__(self, *, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _AccessRejectedError(_SmartsheetError):
    """Smartsheet refused the bearer token with HTTP 401."""


def _invalid(message: str) -> _SmartsheetError:
    return _SmartsheetError(code="invalid_argument", message=message)


def _positive_id(value: object, field_name: str) -> int:
    """Return a Smartsheet ID given as a positive integer or a string of digits."""
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{field_name} must be a positive integer Smartsheet ID"
        raise _invalid(msg)
    return value


def _sheet_id(value: object) -> int:
    return _positive_id(value, "sheet_id")


def _row_ids(value: object) -> list[int]:
    """Return between 1 and 100 row IDs."""
    if not isinstance(value, list) or not value:
        msg = "row_ids must be a non-empty list of row IDs"
        raise _invalid(msg)
    if len(value) > _MAX_ROW_IDS:
        msg = f"row_ids holds at most {_MAX_ROW_IDS} IDs per call"
        raise _invalid(msg)
    return [_positive_id(row_id, "row_ids") for row_id in value]


def _paging(page: int, page_size: int) -> dict[str, int]:
    return {"page": max(1, page), "pageSize": min(max(1, page_size), _MAX_PAGE_SIZE)}


def _mapping(value: object) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    return [_mapping(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _error_from_response(response: httpx.Response) -> _SmartsheetError:
    """Return the safe error for a non-2xx response; Smartsheet's own text is redacted and bounded."""
    try:
        body = _mapping(response.json())
    except ValueError:
        body = {}
    details: dict[str, object] = {"status_code": response.status_code}
    if isinstance(body.get("errorCode"), int):
        details["error_code"] = body["errorCode"]
    if isinstance(body.get("refId"), str):
        details["ref_id"] = body["refId"]
    message = body.get("message")
    text = (
        redact_sensitive_text(message, max_length=_MAX_ERROR_MESSAGE_CHARS)
        if isinstance(message, str) and message.strip()
        else f"Smartsheet answered HTTP {response.status_code}"
    )
    if response.status_code == 401:
        return _AccessRejectedError(code="access_rejected", message=text, **details)
    return _SmartsheetError(
        code=_STATUS_ERROR_CODES.get(response.status_code, "provider_error"),
        message=text,
        **details,
    )


def _new_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Build a client that sends the bearer only to the Smartsheet API and never follows a redirect."""
    return httpx.AsyncClient(
        base_url=_API_BASE_URL,
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=transport,
    )


async def _request(
    token: str,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json_body: object = None,
) -> Any:  # noqa: ANN401
    """Send one Smartsheet API request and return its JSON body."""
    try:
        async with _new_http_client() as client:
            response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("smartsheet_request_failed", method=method, error_type=type(exc).__name__)
        raise _SmartsheetError(
            code="network_error",
            message="Smartsheet could not be reached. Retry this request shortly.",
            error_type=type(exc).__name__,
        ) from None
    if not response.is_success:
        error = _error_from_response(response)
        logger.warning(
            "smartsheet_request_failed",
            method=method,
            status_code=response.status_code,
            error_code=error.details.get("error_code"),
        )
        raise error
    try:
        return response.json()
    except ValueError:
        raise _SmartsheetError(
            code="provider_error",
            message="Smartsheet returned a response that is not JSON",
            status_code=response.status_code,
        ) from None


def _refresh_failed(exc: OAuthProviderError) -> _SmartsheetError:
    logger.warning("smartsheet_oauth_refresh_failed", error_type=type(exc).__name__)
    return _SmartsheetError(
        code="oauth_refresh_failed",
        message="Smartsheet authorization could not be refreshed. Retry this request shortly.",
    )


def _column_summary(column: Mapping[str, Any]) -> dict[str, object]:
    summary: dict[str, object] = {"id": column.get("id"), "title": column.get("title"), "type": column.get("type")}
    if column.get("primary") is True:
        summary["primary"] = True
    return summary


def _row_summary(row: Mapping[str, Any], titles: Mapping[object, str]) -> dict[str, object]:
    """Return one row with its non-empty cells keyed by column title."""
    cells: dict[str, object] = {}
    for cell in _items(row.get("cells")):
        title = titles.get(cell.get("columnId"))
        if title is None:
            continue
        if "value" in cell:
            cells[title] = cell["value"]
        elif "displayValue" in cell:
            cells[title] = cell["displayValue"]
    return {"row_id": row.get("id"), "row_number": row.get("rowNumber"), "cells": cells}


class SmartsheetTools(Toolkit):
    """Smartsheet functions authenticated by an API access token or the requester's own OAuth connection."""

    def __init__(
        self,
        access_token: str | None = None,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager,
        worker_target: ResolvedWorkerTarget | None,
        runtime_config: Config | None = None,
    ) -> None:
        super().__init__(name="smartsheet")
        self._api_token = access_token.strip() if isinstance(access_token, str) and access_token.strip() else None
        self._provider = smartsheet_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config

    def _payload(self, status: str, **fields: object) -> str:
        return custom_tool_payload(self.name, status, **fields)

    def _credential_context(self) -> OAuthCredentialContext:
        return active_oauth_credential_context(
            self._provider,
            self._runtime_paths,
            self._credentials_manager,
            self._worker_target,
            config=self._config,
        )

    async def _connection_required(self, *, reason: str | None = None) -> OAuthConnectionRequired:
        # Building the link reads credential state synchronously, so keep it off the event loop.
        return await asyncio.to_thread(oauth_connection_required, self._credential_context(), reason=reason)

    async def _access_token(self) -> str:
        """Return the API token, or the requester's current OAuth token, refreshing one about to expire."""
        if self._api_token is not None:
            return self._api_token
        context = self._credential_context()
        if context.worker_target is None:
            # Requester-only credentials never fall back to a shared or global store.
            raise await self._connection_required()
        token, _credentials = await asyncio.to_thread(current_oauth_access_token, context, _refresh_failed)
        return token

    async def _call(self, operation: _Operation) -> str:
        """Authenticate and run one operation, reducing every failure to a safe payload."""
        try:
            fields = await operation(await self._access_token())
        except OAuthConnectionRequired as exc:
            return self._payload("error", **oauth_connection_required_payload(exc))
        except _AccessRejectedError as exc:
            if self._api_token is not None:
                return self._payload("error", code="access_token_rejected", message=_API_KEY_REJECTED_MESSAGE)
            required = await self._connection_required(reason=OAUTH_ACCESS_REJECTED_REASON)
            return self._payload("error", **oauth_connection_required_payload(required), **exc.details)
        except _SmartsheetError as exc:
            return self._payload("error", code=exc.code, message=exc.message, **exc.details)
        return self._payload("ok", **fields)

    @tool
    async def list_sheets(self, page: int = 1, page_size: int = 100) -> str:
        """List the Smartsheet sheets the connected account can open.

        Args:
            page: Page number, starting at 1.
            page_size: Sheets per page, at most 500.

        """

        async def operation(token: str) -> dict[str, object]:
            data = _mapping(await _request(token, "GET", "/sheets", params=_paging(page, page_size)))
            return {
                "page": data.get("pageNumber"),
                "total_pages": data.get("totalPages"),
                "total_count": data.get("totalCount"),
                "sheets": [
                    {
                        "id": sheet.get("id"),
                        "name": sheet.get("name"),
                        "access_level": sheet.get("accessLevel"),
                        "modified_at": sheet.get("modifiedAt"),
                        "permalink": sheet.get("permalink"),
                    }
                    for sheet in _items(data.get("data"))
                ],
            }

        return await self._call(operation)

    @tool
    async def search_sheets(self, query: str) -> str:
        """Search every sheet the connected account can open for text in sheet names, rows, and attachments.

        Args:
            query: Text to search for.

        """

        async def operation(token: str) -> dict[str, object]:
            if not query.strip():
                msg = "query must not be empty"
                raise _invalid(msg)
            data = _mapping(await _request(token, "GET", "/search", params={"query": query}))
            return {
                "total_count": data.get("totalCount"),
                "results": [
                    {
                        "object_type": hit.get("objectType"),
                        "object_id": hit.get("objectId"),
                        "parent_object_id": hit.get("parentObjectId"),
                        "parent_object_name": hit.get("parentObjectName"),
                        "parent_object_type": hit.get("parentObjectType"),
                        "text": hit.get("text"),
                        "context": hit.get("contextData"),
                    }
                    for hit in _items(data.get("results"))
                ],
            }

        return await self._call(operation)

    @tool
    async def get_sheet(
        self,
        sheet_id: int | str,
        row_ids: list[int | str] | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> str:
        """Read a sheet's columns and rows, with each row's cells keyed by column title; empty cells are left out.

        Args:
            sheet_id: The sheet's ID.
            row_ids: Read only these rows, at most 100.
            page: Row page number, starting at 1.
            page_size: Rows per page, at most 500.

        """

        async def operation(token: str) -> dict[str, object]:
            params: dict[str, str | int] = {**_paging(page, page_size)}
            if row_ids is not None:
                params["rowIds"] = ",".join(str(row_id) for row_id in _row_ids(row_ids))
            sheet = _mapping(await _request(token, "GET", f"/sheets/{_sheet_id(sheet_id)}", params=params))
            columns = _items(sheet.get("columns"))
            titles = {column.get("id"): column["title"] for column in columns if isinstance(column.get("title"), str)}
            return {
                "id": sheet.get("id"),
                "name": sheet.get("name"),
                "permalink": sheet.get("permalink"),
                "total_row_count": sheet.get("totalRowCount"),
                "columns": [_column_summary(column) for column in columns],
                "rows": [_row_summary(row, titles) for row in _items(sheet.get("rows"))],
            }

        return await self._call(operation)
