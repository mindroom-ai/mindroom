"""Tests for the Google Sheets OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.google_sheets import GoogleSheetsTools
from mindroom.oauth.google import google_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target
from tests.agent_tool_helpers import toolkit_offers
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_runtime_paths(
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://mindroom.example.test",
            **(extra_env or {}),
        },
    )


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    return resolve_worker_target("user_agent", "general", execution_identity=identity)


def test_google_sheets_missing_credentials_raises_structured_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    with pytest.raises(OAuthConnectionRequired) as exc_info:
        tool._authenticate()

    assert exc_info.value.provider_id == "google_sheets"
    assert exc_info.value.connect_url is not None
    assert "/api/oauth/google_sheets/authorize?connect_token=" in exc_info.value.connect_url
    assert "@alice:example.org" not in str(exc_info.value)


def test_google_sheets_public_method_returns_structured_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    result = json.loads(tool.read_sheet(spreadsheet_id="sheet-id", spreadsheet_range="A1:B2"))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_sheets"
    assert "/api/oauth/google_sheets/authorize?connect_token=" in result["connect_url"]

    writable_tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
        create=True,
        update=True,
    )
    result = json.loads(writable_tool.create_sheet("Budget"))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_sheets"
    assert "/api/oauth/google_sheets/authorize?connect_token=" in result["connect_url"]

    result = json.loads(writable_tool.batch_update_sheet("sheet-id", []))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_sheets"


def test_google_sheets_loads_tokens_from_oauth_service(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials("google_sheets", {"spreadsheet_id": "sheet-id", "_source": "ui"})
    publish_oauth_credentials(
        google_oauth_provider("google_sheets"),
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "spreadsheet_id" not in token_data


def test_google_sheets_saved_dashboard_config_maps_to_upstream_init_args(tmp_path: Path) -> None:
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_sheets",
        {
            "read": False,
            "create": True,
            "update": True,
            "_source": "ui",
        },
    )

    tool = get_tool_by_name(
        "google_sheets",
        _runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleSheetsTools)
    assert list(tool.functions) == [
        "create_sheet",
        "update_sheet",
        "batch_update_sheet",
    ]


def test_google_sheets_disabled_update_omits_batch_update(tmp_path: Path) -> None:
    tool = get_tool_by_name(
        "google_sheets",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        tool_config_overrides={"update": False},
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleSheetsTools)
    assert "batch_update_sheet" not in tool.functions


def test_google_sheets_default_config_enables_read_and_write_methods(tmp_path: Path) -> None:
    tool = get_tool_by_name(
        "google_sheets",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )

    assert isinstance(tool, GoogleSheetsTools)
    assert set(tool.functions) == {
        "read_sheet",
        "create_sheet",
        "update_sheet",
        "batch_update_sheet",
    }


class _FakeSheetsRequest:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def execute(self) -> dict[str, object]:
        return self._response


class _FakeValuesResource:
    def __init__(self) -> None:
        self.get_kwargs: dict[str, object] | None = None
        self.update_kwargs: dict[str, object] | None = None

    def get(self, **kwargs: object) -> _FakeSheetsRequest:
        self.get_kwargs = kwargs
        return _FakeSheetsRequest({"values": [["ok"]]})

    def update(self, **kwargs: object) -> _FakeSheetsRequest:
        self.update_kwargs = kwargs
        return _FakeSheetsRequest({"updatedRange": kwargs["range"]})


class _FakeSpreadsheetsResource:
    def __init__(self) -> None:
        self.batch_update_kwargs: dict[str, object] | None = None
        self.batch_update_error: HttpError | None = None
        self.values_resource = _FakeValuesResource()

    def values(self) -> _FakeValuesResource:
        return self.values_resource

    def batchUpdate(self, **kwargs: object) -> _FakeSheetsRequest:  # noqa: N802
        self.batch_update_kwargs = kwargs
        if self.batch_update_error is not None:
            raise self.batch_update_error
        return _FakeSheetsRequest({"spreadsheetId": kwargs["spreadsheetId"], "replies": [{}]})


class _FakeSheetsService:
    def __init__(self) -> None:
        self.spreadsheets_resource = _FakeSpreadsheetsResource()

    def spreadsheets(self) -> _FakeSpreadsheetsResource:
        return self.spreadsheets_resource


def _connected_sheets_tool(
    tmp_path: Path,
    *,
    spreadsheet_id: str | None = None,
    spreadsheet_range: str | None = None,
) -> tuple[GoogleSheetsTools, _FakeSheetsService]:
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        spreadsheet_id=spreadsheet_id,
        spreadsheet_range=spreadsheet_range,
        creds=GoogleOAuthCredentials(
            token="valid-access-token",  # noqa: S106
            refresh_token="valid-refresh-token",  # noqa: S106
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
            client_id="client-id",
            client_secret="client-secret",  # noqa: S106
            scopes=("https://www.googleapis.com/auth/spreadsheets",),
            expiry=datetime(2100, 1, 1),  # noqa: DTZ001
        ),
    )
    service = _FakeSheetsService()
    tool.service = service
    return tool, service


_FORMAT_REQUESTS: list[dict[str, object]] = [
    {
        "updateSheetProperties": {
            "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        },
    },
    {
        "repeatCell": {
            "range": {"sheetId": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        },
    },
]


def test_google_sheets_batch_update_sends_requests_in_order(tmp_path: Path) -> None:
    tool, service = _connected_sheets_tool(tmp_path)

    result = json.loads(tool.batch_update_sheet("sheet-id", _FORMAT_REQUESTS))

    assert result == {"spreadsheetId": "sheet-id", "replies": [{}]}
    assert service.spreadsheets_resource.batch_update_kwargs == {
        "spreadsheetId": "sheet-id",
        "body": {"requests": _FORMAT_REQUESTS},
    }


def test_google_sheets_schemas_accept_sheets_api_requests_and_cell_values(tmp_path: Path) -> None:
    tool, _service = _connected_sheets_tool(tmp_path)

    validate_tool_arguments(
        dict(toolkit_offers(tool, "batch_update_sheet").parameters),
        {"spreadsheet_id": "sheet-id", "requests": _FORMAT_REQUESTS},
    )
    validate_tool_arguments(
        dict(toolkit_offers(tool, "update_sheet").parameters),
        {"spreadsheet_id": "sheet-id", "range_name": "A1:C2", "data": [["Item", "Count", "Done"], ["lamp", 2, True]]},
    )


def test_google_sheets_service_account_env_uses_upstream_auth(tmp_path: Path) -> None:
    tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(
            tmp_path,
            {"GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "service-account.json")},
        ),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )

    assert tool._should_fallback_to_original_auth() is True


def test_google_sheets_batch_update_returns_sanitized_http_error(tmp_path: Path) -> None:
    tool, service = _connected_sheets_tool(tmp_path)
    service.spreadsheets_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"provider detail"}}',
    )

    result = json.loads(tool.batch_update_sheet("sheet-id", [{"deleteSheet": {"sheetId": 7}}]))

    assert result == {"error": "Google Sheets request failed (HTTP 400)"}


def test_google_sheets_update_sheet_targets_explicit_call_args_over_configured_read_defaults(tmp_path: Path) -> None:
    tool, service = _connected_sheets_tool(
        tmp_path,
        spreadsheet_id="read-sheet-id",
        spreadsheet_range="ReadSheet!A1:B2",
    )

    write_result = tool.update_sheet(
        data=[["Item", 42]],
        spreadsheet_id="write-sheet-id",
        range_name="WriteSheet!C3:D4",
    )

    assert write_result == "Sheet updated successfully: write-sheet-id"
    assert service.spreadsheets_resource.values_resource.update_kwargs == {
        "spreadsheetId": "write-sheet-id",
        "range": "WriteSheet!C3:D4",
        "valueInputOption": "RAW",
        "body": {"values": [["Item", 42]]},
    }

    read_result = json.loads(tool.read_sheet(spreadsheet_id="ignored-read-id", spreadsheet_range="Ignored!Z9"))

    assert read_result == [["ok"]]
    assert service.spreadsheets_resource.values_resource.get_kwargs == {
        "spreadsheetId": "read-sheet-id",
        "range": "ReadSheet!A1:B2",
    }


@pytest.mark.parametrize(
    ("spreadsheet_id", "range_name"),
    [
        (None, None),
        ("write-sheet-id", None),
        (None, "WriteSheet!C3:D4"),
        ("   ", "WriteSheet!C3:D4"),
        ("write-sheet-id", "   "),
    ],
)
def test_google_sheets_update_sheet_requires_explicit_args_even_with_read_defaults(
    tmp_path: Path,
    spreadsheet_id: str | None,
    range_name: str | None,
) -> None:
    tool, service = _connected_sheets_tool(
        tmp_path,
        spreadsheet_id="read-sheet-id",
        spreadsheet_range="ReadSheet!A1:B2",
    )

    result = tool.update_sheet(data=[["Item", 42]], spreadsheet_id=spreadsheet_id, range_name=range_name)

    assert "must be provided" in result
    assert service.spreadsheets_resource.values_resource.update_kwargs is None


def test_google_sheets_readonly_scopes_reject_write_operations_even_with_write_creds(tmp_path: Path) -> None:
    write_capable_creds = GoogleOAuthCredentials(
        token="valid-access-token",  # noqa: S106
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("https://www.googleapis.com/auth/spreadsheets",),
        expiry=datetime(2100, 1, 1),  # noqa: DTZ001
    )

    with pytest.raises(ValueError, match=r"https://www\.googleapis\.com/auth/spreadsheets"):
        GoogleSheetsTools(
            runtime_paths=_runtime_paths(tmp_path),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            creds=write_capable_creds,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )

    read_only_tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=write_capable_creds,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        create=False,
        update=False,
    )
    assert set(read_only_tool.functions) == {"read_sheet"}

    writable_tool = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        creds=write_capable_creds,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    assert "update_sheet" in writable_tool.functions


def test_google_sheets_explicit_none_toggles_disable_operations(tmp_path: Path) -> None:
    direct_read_only = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        create=None,
        update=None,
    )
    assert set(direct_read_only.functions) == {"read_sheet"}
    assert direct_read_only.scopes == ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    direct_alias_read_only = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        create_sheet=None,
        update_sheet=None,
    )
    assert set(direct_alias_read_only.functions) == {"read_sheet"}

    direct_all_disabled = GoogleSheetsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        read=None,
        create=None,
        update=None,
        create_duplicate_sheet=None,
    )
    assert direct_all_disabled.functions == {}
    assert direct_all_disabled.scopes == []

    with pytest.raises(ValueError, match="received both 'create' and 'create_sheet'"):
        GoogleSheetsTools(
            runtime_paths=_runtime_paths(tmp_path),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            create=None,
            create_sheet=None,
        )

    override_tool = get_tool_by_name(
        "google_sheets",
        _runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        tool_config_overrides={"create": None, "update": None},
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )
    assert isinstance(override_tool, GoogleSheetsTools)
    assert set(override_tool.functions) == {"read_sheet"}

    credentials_manager = CredentialsManager(tmp_path / "credentials")
    credentials_manager.save_credentials(
        "google_sheets",
        {"create": None, "update": None, "_source": "ui"},
    )
    saved_config_tool = get_tool_by_name(
        "google_sheets",
        _runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=None,
        runs_in_worker=False,
        tools=builtin_tool_registry(),
    )
    assert isinstance(saved_config_tool, GoogleSheetsTools)
    assert set(saved_config_tool.functions) == {"read_sheet"}
