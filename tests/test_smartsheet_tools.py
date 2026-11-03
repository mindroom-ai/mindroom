"""Tests for the Smartsheet toolkit's API access token, requester OAuth, and Smartsheet API calls."""

from __future__ import annotations

import json
import time
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import smartsheet as smartsheet_tools
from mindroom.custom_tools.smartsheet import SmartsheetTools
from mindroom.oauth.service import OAUTH_ACCESS_REJECTED_REASON
from mindroom.oauth.smartsheet import smartsheet_oauth_provider
from tests.atlassian_test_support import grant, runtime_paths, worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager

_TARGET = object()
_API_TOKEN = "api-token"  # noqa: S105
_BLANK_TOKEN = "   "  # noqa: S105
_SHEET = {
    "id": 123,
    "name": "Launch",
    "permalink": "https://app.smartsheet.com/sheets/abc",
    "totalRowCount": 1,
    "columns": [
        {"id": 1, "index": 0, "title": "Task", "type": "TEXT_NUMBER", "primary": True},
        {"id": 2, "index": 1, "title": "Done", "type": "CHECKBOX"},
        {"id": 3, "index": 2, "title": "Owner", "type": "CONTACT_LIST"},
        {"id": 4, "index": 3, "title": "Notes", "type": "TEXT_NUMBER"},
    ],
    "rows": [
        {
            "id": 9,
            "rowNumber": 1,
            "cells": [
                {"columnId": 1, "value": "Ship", "displayValue": "Ship"},
                {"columnId": 2, "value": True},
                {"columnId": 3, "displayValue": "Alice"},
                {"columnId": 4},
            ],
        },
    ],
}


def _runtime(tmp_path: Path) -> tuple[RuntimePaths, CredentialsManager]:
    paths = runtime_paths(tmp_path)
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials(
        "smartsheet_oauth_client",
        {"client_id": "smartsheet-client", "client_secret": "smartsheet-secret"},
    )
    return paths, manager


def _connect(manager: CredentialsManager, token: str = "oauth-token", **overrides: object) -> None:  # noqa: S107
    provider = smartsheet_oauth_provider()
    publish_oauth_credentials(
        provider,
        grant(token, provider, client_id="smartsheet-client", **overrides),
        credentials_manager=manager,
        worker_target=worker_target(),
    )


def _tools(
    paths: RuntimePaths,
    manager: CredentialsManager,
    *,
    access_token: str | None = None,
    target: object = _TARGET,
) -> SmartsheetTools:
    return SmartsheetTools(
        access_token=access_token,
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target() if target is _TARGET else None,
    )


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Answer the Smartsheet API with ``handler``, recording every request the toolkit sends."""
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        smartsheet_tools,
        "_new_http_client",
        partial(smartsheet_tools._new_http_client, transport=httpx.MockTransport(record)),
    )
    return requests


def _json(status_code: int, payload: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status_code, json=payload)


@pytest.mark.asyncio
async def test_api_token_reads_sheet_rows_by_column_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An API access token is sent as the bearer, and each row's cells are keyed by column title."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, _SHEET))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).get_sheet("123"))

    (request,) = requests
    assert request.headers["authorization"] == "Bearer api-token"
    assert str(request.url).startswith("https://api.smartsheet.com/2.0/sheets/123?")
    assert result["status"] == "ok"
    assert result["name"] == "Launch"
    assert result["total_row_count"] == 1
    assert result["columns"][0] == {"id": 1, "title": "Task", "type": "TEXT_NUMBER", "primary": True}
    assert result["rows"][0]["row_id"] == 9
    assert result["rows"][0]["row_number"] == 1
    assert result["rows"][0]["cells"]["Task"] == "Ship"
    assert result["rows"][0]["cells"]["Done"] is True


@pytest.mark.asyncio
async def test_cell_without_value_falls_back_to_display_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cell with only a display value shows it, and an empty cell is left out."""
    paths, manager = _runtime(tmp_path)
    _serve(monkeypatch, _json(200, _SHEET))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).get_sheet(123))

    assert result["rows"][0]["cells"] == {"Task": "Ship", "Done": True, "Owner": "Alice"}


@pytest.mark.asyncio
async def test_get_sheet_sends_row_ids_and_paging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Row IDs and paging reach Smartsheet as its query parameters."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, _SHEET))

    await _tools(paths, manager, access_token=_API_TOKEN).get_sheet(123, row_ids=[9, "10"], page=2, page_size=50)

    params = requests[0].url.params
    assert params["rowIds"] == "9,10"
    assert params["page"] == "2"
    assert params["pageSize"] == "50"


@pytest.mark.asyncio
async def test_list_sheets_clamps_page_size_and_reports_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized page is clamped to Smartsheet's 500 and the listing reports its paging."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(
        monkeypatch,
        _json(
            200,
            {
                "pageNumber": 1,
                "pageSize": 500,
                "totalPages": 3,
                "totalCount": 1200,
                "data": [
                    {
                        "id": 1,
                        "name": "Plan",
                        "accessLevel": "OWNER",
                        "modifiedAt": "2026-11-01T10:00:00Z",
                        "permalink": "https://app.smartsheet.com/sheets/plan",
                    },
                ],
            },
        ),
    )

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).list_sheets(page_size=10000))

    assert requests[0].url.path == "/2.0/sheets"
    assert requests[0].url.params["pageSize"] == "500"
    assert result["total_pages"] == 3
    assert result["total_count"] == 1200
    assert result["sheets"] == [
        {
            "id": 1,
            "name": "Plan",
            "access_level": "OWNER",
            "modified_at": "2026-11-01T10:00:00Z",
            "permalink": "https://app.smartsheet.com/sheets/plan",
        },
    ]


@pytest.mark.asyncio
async def test_search_sheets_maps_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Search hits name the object, the sheet it belongs to, and the matching text."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(
        monkeypatch,
        _json(
            200,
            {
                "totalCount": 1,
                "results": [
                    {
                        "objectType": "row",
                        "objectId": 9,
                        "parentObjectId": 123,
                        "parentObjectName": "Launch",
                        "parentObjectType": "sheet",
                        "text": "Ship launch",
                        "contextData": ["Row 1"],
                    },
                ],
            },
        ),
    )

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).search_sheets("launch"))

    assert requests[0].url.path == "/2.0/search"
    assert requests[0].url.params["query"] == "launch"
    assert result["total_count"] == 1
    assert result["results"] == [
        {
            "object_type": "row",
            "object_id": 9,
            "parent_object_id": 123,
            "parent_object_name": "Launch",
            "parent_object_type": "sheet",
            "text": "Ship launch",
            "context": ["Row 1"],
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("sheet_id", ["abc", "", "-5", 0, True, 1.5])
async def test_invalid_sheet_id_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sheet_id: object,
) -> None:
    """A sheet ID that is not a positive integer is refused before any request."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, _SHEET))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).get_sheet(sheet_id))  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert result["code"] == "invalid_argument"
    assert requests == []


@pytest.mark.asyncio
async def test_more_than_100_row_ids_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At most 100 row IDs fit one call."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, _SHEET))

    result = json.loads(
        await _tools(paths, manager, access_token=_API_TOKEN).get_sheet(1, row_ids=list(range(1, 102))),
    )

    assert result["code"] == "invalid_argument"
    assert requests == []


@pytest.mark.asyncio
async def test_requester_without_connection_gets_connect_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an API token or a connection, the requester gets their own connect link."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, {"data": []}))

    result = json.loads(await _tools(paths, manager).list_sheets())

    assert result["status"] == "error"
    assert result["oauth_connection_required"] is True
    assert result["provider"] == "smartsheet"
    assert "/api/oauth/smartsheet/authorize?connect_token=" in result["connect_url"]
    assert requests == []


@pytest.mark.asyncio
async def test_no_worker_target_never_uses_a_requester_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call without a requester never falls back to someone's stored connection."""
    paths, manager = _runtime(tmp_path)
    _connect(manager)
    requests = _serve(monkeypatch, _json(200, {"data": []}))

    result = json.loads(await _tools(paths, manager, target=None).list_sheets())

    assert result["oauth_connection_required"] is True
    assert requests == []


@pytest.mark.asyncio
async def test_connected_requester_sends_oauth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connected requester's OAuth token is the bearer when no API token is configured."""
    paths, manager = _runtime(tmp_path)
    _connect(manager)
    requests = _serve(monkeypatch, _json(200, {"data": []}))

    result = json.loads(await _tools(paths, manager).list_sheets())

    assert result["status"] == "ok"
    assert requests[0].headers["authorization"] == "Bearer oauth-token"


@pytest.mark.asyncio
async def test_blank_access_token_falls_through_to_oauth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only API token counts as unset."""
    paths, manager = _runtime(tmp_path)
    _connect(manager)
    requests = _serve(monkeypatch, _json(200, {"data": []}))

    await _tools(paths, manager, access_token=_BLANK_TOKEN).list_sheets()

    assert requests[0].headers["authorization"] == "Bearer oauth-token"


@pytest.mark.asyncio
async def test_expiring_oauth_token_is_refreshed_before_the_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token about to expire is refreshed with the client secret in the form body, then sent."""
    paths, manager = _runtime(tmp_path)
    _connect(manager, token="stale-token", expires_at=time.time() + 5)  # noqa: S106
    token_requests: list[httpx.Request] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        token_requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "fresh-token", "token_type": "bearer", "expires_in": 604799, "refresh_token": "r2"},
        )

    class _TokenClient(AsyncOAuth2Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs, transport=httpx.MockTransport(token_endpoint))

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _TokenClient)
    requests = _serve(monkeypatch, _json(200, {"data": []}))

    await _tools(paths, manager).list_sheets()

    (token_request,) = token_requests
    assert str(token_request.url) == "https://api.smartsheet.com/2.0/token"
    form = parse_qs(token_request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["stale-token-refresh"]
    assert form["client_secret"] == ["smartsheet-secret"]
    assert requests[0].headers["authorization"] == "Bearer fresh-token"


@pytest.mark.asyncio
async def test_rejected_oauth_token_asks_for_reconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Smartsheet refusing the requester's OAuth token turns into a reconnect prompt."""
    paths, manager = _runtime(tmp_path)
    _connect(manager)
    _serve(monkeypatch, _json(401, {"errorCode": 1002, "message": "Your Access Token is invalid.", "refId": "x"}))

    result = json.loads(await _tools(paths, manager).list_sheets())

    assert result["oauth_connection_required"] is True
    assert result["reason"] == OAUTH_ACCESS_REJECTED_REASON


@pytest.mark.asyncio
async def test_rejected_api_token_reports_access_token_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smartsheet refusing the configured API token is an error, never a connect prompt."""
    paths, manager = _runtime(tmp_path)
    _serve(monkeypatch, _json(401, {"errorCode": 1002, "message": "Your Access Token is invalid.", "refId": "x"}))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).list_sheets())

    assert result["status"] == "error"
    assert result["code"] == "access_token_rejected"
    assert "oauth_connection_required" not in result


@pytest.mark.asyncio
async def test_provider_error_message_is_redacted_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smartsheet's error text is redacted, cut to 300 characters, and kept with its code and reference."""
    paths, manager = _runtime(tmp_path)
    message = "denied for token sk-abc123DEF456ghi789JKL " + "x" * 1000
    _serve(monkeypatch, _json(403, {"errorCode": 1004, "message": message, "refId": "r1"}))

    raw = await _tools(paths, manager, access_token=_API_TOKEN).get_sheet(123)

    result = json.loads(raw)
    assert result["code"] == "permission_denied"
    assert result["status_code"] == 403
    assert result["error_code"] == 1004
    assert result["ref_id"] == "r1"
    assert len(result["message"]) <= 300
    assert "sk-abc123DEF456ghi789JKL" not in raw


@pytest.mark.asyncio
async def test_html_error_body_reports_provider_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy's HTML error page is reported by its status alone."""
    paths, manager = _runtime(tmp_path)
    _serve(monkeypatch, lambda _request: httpx.Response(502, text="<html>Bad gateway</html>"))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).list_sheets())

    assert result["code"] == "provider_error"
    assert result["status_code"] == 502
    assert "html" not in json.dumps(result).lower()


@pytest.mark.asyncio
async def test_network_failure_reports_type_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport failure reports its exception type, never its text."""
    paths, manager = _runtime(tmp_path)

    def fail(_request: httpx.Request) -> httpx.Response:
        msg = "boom with api-token"
        raise httpx.ConnectError(msg)

    _serve(monkeypatch, fail)

    raw = await _tools(paths, manager, access_token=_API_TOKEN).list_sheets()

    result = json.loads(raw)
    assert result["code"] == "network_error"
    assert result["error_type"] == "ConnectError"
    assert "api-token" not in raw


def _sheet_api(write_response: object) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the column listing with the test sheet's columns and any write with ``write_response``."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/2.0/sheets/123/columns":
            return httpx.Response(200, json={"data": _SHEET["columns"]})
        return httpx.Response(200, json=write_response)

    return handle


def _added(row_id: int, row_number: int) -> dict[str, object]:
    return {"message": "SUCCESS", "resultCode": 0, "result": [{"id": row_id, "rowNumber": row_number}]}


@pytest.mark.asyncio
async def test_add_rows_maps_titles_to_column_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """New rows name cells by column title and reach Smartsheet as column IDs at the bottom of the sheet."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(77, 4)))

    result = json.loads(
        await _tools(paths, manager, access_token=_API_TOKEN).add_rows("123", [{"Task": "Ship", "Done": False}]),
    )

    columns_request, write_request = requests
    assert columns_request.url.params["includeAll"] == "true"
    assert write_request.method == "POST"
    assert write_request.url.path == "/2.0/sheets/123/rows"
    assert json.loads(write_request.content) == [
        {"toBottom": True, "cells": [{"columnId": 1, "value": "Ship"}, {"columnId": 2, "value": False}]},
    ]
    assert result["status"] == "ok"
    assert result["rows"] == [{"row_id": 77, "row_number": 4}]


@pytest.mark.asyncio
async def test_add_rows_to_top(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows can go to the top of the sheet instead."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(77, 1)))

    await _tools(paths, manager, access_token=_API_TOKEN).add_rows(123, [{"Task": "Ship"}], to_top=True)

    assert json.loads(requests[1].content) == [{"toTop": True, "cells": [{"columnId": 1, "value": "Ship"}]}]


@pytest.mark.asyncio
async def test_unknown_column_title_sends_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A title the sheet lacks is refused with the sheet's titles, and nothing is written."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(77, 4)))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).add_rows(123, [{"Nope": 1}]))

    assert result["code"] == "invalid_argument"
    assert "Nope" in result["message"]
    assert result["available_columns"] == ["Task", "Done", "Owner", "Notes"]
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_update_rows_puts_row_ids_and_cells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Updates name each row by ID and its changed cells by column title."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(9, 1)))

    result = json.loads(
        await _tools(paths, manager, access_token=_API_TOKEN).update_rows(
            "123",
            [{"row_id": "9", "cells": {"Done": True}}],
        ),
    )

    write_request = requests[1]
    assert write_request.method == "PUT"
    assert write_request.url.path == "/2.0/sheets/123/rows"
    assert json.loads(write_request.content) == [{"id": 9, "cells": [{"columnId": 2, "value": True}]}]
    assert result["rows"] == [{"row_id": 9, "row_number": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"cells": {"Done": True}}],
        [{"row_id": 9}],
        [{"row_id": 9, "cells": {}}],
        [{"row_id": "x", "cells": {"Done": True}}],
        [],
    ],
)
async def test_malformed_update_sends_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    """An update without a valid row ID or any cells is refused before any request."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(9, 1)))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).update_rows(123, rows))

    assert result["code"] == "invalid_argument"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{}]])
async def test_empty_new_rows_send_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> None:
    """Adding no rows, or a row without cells, is refused before any request."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _sheet_api(_added(77, 4)))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).add_rows(123, rows))

    assert result["code"] == "invalid_argument"
    assert requests == []


@pytest.mark.asyncio
async def test_delete_rows_refuses_missing_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deletes send every row ID and ask Smartsheet to delete nothing if any is missing."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, {"message": "SUCCESS", "resultCode": 0, "result": [9, 10]}))

    result = json.loads(await _tools(paths, manager, access_token=_API_TOKEN).delete_rows(123, [9, "10"]))

    (request,) = requests
    assert request.method == "DELETE"
    assert request.url.path == "/2.0/sheets/123/rows"
    assert request.url.params["ids"] == "9,10"
    assert request.url.params["ignoreRowsNotFound"] == "false"
    assert result["deleted_row_ids"] == [9, 10]


@pytest.mark.asyncio
async def test_delete_more_than_100_rows_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At most 100 rows are deleted per call."""
    paths, manager = _runtime(tmp_path)
    requests = _serve(monkeypatch, _json(200, {"result": []}))

    result = json.loads(
        await _tools(paths, manager, access_token=_API_TOKEN).delete_rows(123, list(range(1, 102))),
    )

    assert result["code"] == "invalid_argument"
    assert requests == []
