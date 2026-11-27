"""Tests for the Google Docs OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from mindroom import constants
from mindroom import tools as _mindroom_tools  # noqa: F401  # registers built-in tool metadata
from mindroom.config.main import Config
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools.google_docs import GoogleDocsTools
from mindroom.oauth.google import google_oauth_provider
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.toolkits import rename_toolkit_functions
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.agent_tool_helpers import toolkit_offers
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


def _valid_credentials() -> GoogleOAuthCredentials:
    return GoogleOAuthCredentials(
        token="valid-access-token",  # noqa: S106
        refresh_token="valid-refresh-token",  # noqa: S106
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id="client-id",
        client_secret="client-secret",  # noqa: S106
        scopes=("scope",),
        expiry=datetime(2100, 1, 1, tzinfo=UTC),
    )


class _FakeDocsRequest:
    def __init__(self, response: dict[str, object], error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeDocumentsResource:
    def __init__(self) -> None:
        self.create_bodies: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.batch_update_calls: list[dict[str, object]] = []
        self.batch_update_error: Exception | None = None

    def create(self, *, body: dict[str, object]) -> _FakeDocsRequest:
        self.create_bodies.append(body)
        return _FakeDocsRequest(
            {
                "documentId": "created-doc",
                "title": body["title"],
            },
        )

    def get(self, **kwargs: object) -> _FakeDocsRequest:
        self.get_calls.append(kwargs)
        return _FakeDocsRequest(
            {
                "documentId": kwargs["documentId"],
                "title": "Plan",
                "tabs": [
                    {
                        "tabProperties": {"tabId": "tab-1", "title": "Tab 1"},
                        "documentTab": {
                            "body": {
                                "content": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": 6,
                                        "paragraph": {
                                            "elements": [
                                                {
                                                    "textRun": {"content": "Hello"},
                                                },
                                            ],
                                        },
                                    },
                                ],
                            },
                        },
                    },
                ],
            },
        )

    def batchUpdate(self, **kwargs: object) -> _FakeDocsRequest:  # noqa: N802
        self.batch_update_calls.append(kwargs)
        return _FakeDocsRequest(
            {
                "documentId": kwargs["documentId"],
                "replies": [{"replaceAllText": {"occurrencesChanged": 2}}],
            },
            self.batch_update_error,
        )


class _FakeDocsService:
    def __init__(self) -> None:
        self.documents_resource = _FakeDocumentsResource()

    def documents(self) -> _FakeDocumentsResource:
        return self.documents_resource


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


def _connected_tool(tmp_path: Path, **kwargs: object) -> tuple[GoogleDocsTools, _FakeDocsService]:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        **kwargs,
    )
    service = _FakeDocsService()
    tool.service = service
    return tool, service


def test_google_docs_missing_credentials_returns_scoped_connect_instruction(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    result = json.loads(tool.google_docs_get_document("document-id"))

    assert result["oauth_connection_required"] is True
    assert result["provider"] == "google_docs"
    assert "https://mindroom.example.test/api/oauth/google_docs/authorize?connect_token=" in result["connect_url"]
    assert "@alice:example.org" not in result["connect_url"]


def test_google_docs_tokens_stay_separate_from_dashboard_settings(tmp_path: Path) -> None:
    manager = CredentialsManager(tmp_path / "credentials")
    manager.save_credentials("google_docs", {"edit_document": False, "_source": "ui"})
    publish_oauth_credentials(
        google_oauth_provider("google_docs"),
        {"token": "access-token", "refresh_token": "refresh-token", "_source": "oauth"},
        credentials_manager=manager,
        worker_target=None,
    )
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=manager,
        worker_target=None,
    )

    token_data = tool._load_token_data()

    assert token_data is not None
    assert token_data["token"] == "access-token"  # noqa: S105
    assert "edit_document" not in token_data


def test_google_docs_create_document_can_insert_initial_text(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_write(action="create", title="Launch plan", initial_text="First draft"))

    assert service.documents_resource.create_bodies == [{"title": "Launch plan"}]
    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "created-doc",
            "body": {
                "requests": [
                    {
                        "insertText": {
                            "endOfSegmentLocation": {},
                            "text": "First draft",
                        },
                    },
                ],
            },
        },
    ]
    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert result["initialTextUpdate"]["documentId"] == "created-doc"


def test_google_docs_create_document_preserves_id_when_initial_text_fails(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"bad request"}}',
    )

    result = json.loads(tool.google_docs_write(action="create", title="Launch plan", initial_text="First draft"))

    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert "400" in result["initialTextError"]


def test_google_docs_create_document_preserves_id_after_initial_text_transport_failure(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    sentinel = "provider-controlled-transport-secret"
    service.documents_resource.batch_update_error = TimeoutError(sentinel)

    result = json.loads(tool.google_docs_write(action="create", title="Launch plan", initial_text="First draft"))

    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert result["initialTextError"] == "Google Docs initial text update failed"
    assert result["partial_success"] is True
    assert result["retry_safe"] is False
    assert sentinel not in json.dumps(result)


def test_google_docs_get_document_returns_tab_aware_structure(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_get_document("document-id"))

    assert service.documents_resource.get_calls == [
        {
            "documentId": "document-id",
            "includeTabsContent": True,
        },
    ]
    assert result["document"]["tabs"][0]["tabProperties"]["tabId"] == "tab-1"
    assert (
        result["document"]["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"][
            "content"
        ]
        == "Hello"
    )


def test_google_docs_get_document_accepts_full_edit_url(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_docs_get_document(
        "https://docs.google.com/document/u/0/d/document-id/edit?usp=sharing",
    )

    assert service.documents_resource.get_calls == [
        {
            "documentId": "document-id",
            "includeTabsContent": True,
        },
    ]


def test_google_docs_insert_text_supports_index_and_tab(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_write(
            action="insert_text",
            document_id="document-id",
            text="new text",
            index=6,
            tab_id="tab-2",
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 6, "tabId": "tab-2"},
                            "text": "new text",
                        },
                    },
                ],
            },
        },
    ]
    assert result["update"]["documentId"] == "document-id"


def test_google_docs_insert_text_appends_when_index_is_omitted(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_docs_write(action="insert_text", document_id="document-id", text="append", tab_id="tab-2")

    assert service.documents_resource.batch_update_calls[0]["body"] == {
        "requests": [
            {
                "insertText": {
                    "endOfSegmentLocation": {"tabId": "tab-2"},
                    "text": "append",
                },
            },
        ],
    }


def test_google_docs_replace_text_can_target_selected_tabs(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_write(
            action="replace_text",
            document_id="document-id",
            find_text="DRAFT",
            replace_text="Final",
            match_case=True,
            tab_ids=["tab-1", "tab-2"],
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": "DRAFT", "matchCase": True},
                            "replaceText": "Final",
                            "tabsCriteria": {"tabIds": ["tab-1", "tab-2"]},
                        },
                    },
                ],
            },
        },
    ]
    assert result["update"]["replies"][0]["replaceAllText"]["occurrencesChanged"] == 2


def test_google_docs_replace_text_accepts_empty_string_to_delete_and_rejects_omitted(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    omitted = json.loads(tool.google_docs_write(action="replace_text", document_id="document-id", find_text="DRAFT"))
    assert "replace_text" in omitted["error"]
    assert service.documents_resource.batch_update_calls == []

    tool.google_docs_write(action="replace_text", document_id="document-id", find_text="DRAFT", replace_text="")
    assert (
        service.documents_resource.batch_update_calls[0]["body"]["requests"][0]["replaceAllText"]["replaceText"] == ""
    )


_RICH_REQUESTS: list[dict[str, object]] = [
    {"insertTable": {"rows": 2, "columns": 3, "endOfSegmentLocation": {}}},
    {
        "updateParagraphStyle": {
            "range": {"startIndex": 1, "endIndex": 6, "tabId": "tab-2"},
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
            "fields": "namedStyleType",
        },
    },
]


def test_google_docs_batch_update_sends_docs_api_requests_in_order(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    # A copy goes in, so that a tool reordering or changing the list it was given cannot change what is expected.
    result = json.loads(
        tool.google_docs_write(
            action="batch_update",
            document_id="https://docs.google.com/document/d/document-id/edit",
            requests=copy.deepcopy(_RICH_REQUESTS),
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {"documentId": "document-id", "body": {"requests": _RICH_REQUESTS}},
    ]
    assert result["update"]["documentId"] == "document-id"
    assert result["documentUrl"] == "https://docs.google.com/document/d/document-id/edit"


def test_google_docs_batch_update_schema_accepts_docs_api_requests(tmp_path: Path) -> None:
    tool, _service = _connected_tool(tmp_path)

    validate_tool_arguments(
        dict(toolkit_offers(tool, "google_docs_write").parameters),
        {"action": "batch_update", "document_id": "document-id", "requests": _RICH_REQUESTS},
    )


def test_google_docs_batch_update_refuses_an_empty_request_list(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(tool.google_docs_write(action="batch_update", document_id="document-id", requests=[]))

    assert result == {"error": "Google Docs requests must not be empty"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_batch_update_returns_sanitized_http_error(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"provider detail"}}',
    )

    result = json.loads(
        tool.google_docs_write(action="batch_update", document_id="document-id", requests=_RICH_REQUESTS),
    )

    assert result == {"error": "Google Docs request failed (HTTP 400)"}


def test_google_docs_batch_update_omits_write_control_when_required_revision_id_is_none(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    offered = toolkit_offers(tool, "google_docs_write")
    validate_tool_arguments(
        dict(offered.parameters),
        {
            "action": "batch_update",
            "document_id": "document-id",
            "requests": _RICH_REQUESTS,
            "required_revision_id": None,
        },
    )

    result = json.loads(
        offered.entrypoint(
            action="batch_update",
            document_id="document-id",
            requests=copy.deepcopy(_RICH_REQUESTS),
            required_revision_id=None,
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {"documentId": "document-id", "body": {"requests": _RICH_REQUESTS}},
    ]
    assert result["update"]["documentId"] == "document-id"


def test_google_docs_batch_update_with_required_revision_id_sends_write_control(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    offered = toolkit_offers(tool, "google_docs_write")
    validate_tool_arguments(
        dict(offered.parameters),
        {
            "action": "batch_update",
            "document_id": "document-id",
            "requests": _RICH_REQUESTS,
            "required_revision_id": "rev-123",
        },
    )

    result = json.loads(
        offered.entrypoint(
            action="batch_update",
            document_id="document-id",
            requests=copy.deepcopy(_RICH_REQUESTS),
            required_revision_id="  rev-123  ",
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {"requests": _RICH_REQUESTS, "writeControl": {"requiredRevisionId": "rev-123"}},
        },
    ]
    assert result["update"]["documentId"] == "document-id"
    assert result["documentUrl"] == "https://docs.google.com/document/d/document-id/edit"


@pytest.mark.parametrize("blank_revision_id", ["", "   ", "\t\n"])
def test_google_docs_batch_update_rejects_blank_required_revision_id(tmp_path: Path, blank_revision_id: str) -> None:
    tool, service = _connected_tool(tmp_path)
    offered = toolkit_offers(tool, "google_docs_write")

    result = json.loads(
        offered.entrypoint(
            action="batch_update",
            document_id="document-id",
            requests=_RICH_REQUESTS,
            required_revision_id=blank_revision_id,
        ),
    )

    assert result == {"error": "Google Docs required_revision_id must not be empty when provided"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_batch_update_http_400_with_required_revision_id_returns_recovery_guidance(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"provider detail"}}',
    )
    offered = toolkit_offers(tool, "google_docs_write")

    result = json.loads(
        offered.entrypoint(
            action="batch_update",
            document_id="document-id",
            requests=_RICH_REQUESTS,
            required_revision_id="rev-stale",
        ),
    )

    error = result["error"]
    assert error.startswith("Google Docs request failed (HTTP 400). ")
    assert "provider detail" not in error
    assert "required_revision_id" in error
    assert "Re-read the document/tab" in error
    assert "UTF-16 start-inclusive/end-exclusive ranges" in error
    assert "current revisionId" in error


def test_google_docs_batch_update_non_400_http_error_with_required_revision_id_keeps_standard_error(
    tmp_path: Path,
) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "500", "reason": "Internal Server Error"}),
        b'{"error":{"message":"provider detail"}}',
    )
    offered = toolkit_offers(tool, "google_docs_write")

    result = json.loads(
        offered.entrypoint(
            action="batch_update",
            document_id="document-id",
            requests=_RICH_REQUESTS,
            required_revision_id="rev-123",
        ),
    )

    assert result == {"error": "Google Docs request failed (HTTP 500)"}


def test_google_docs_config_flags_control_registered_functions_and_narrow_write_actions(tmp_path: Path) -> None:
    read_only, _ = _connected_tool(tmp_path, create_document=False, read_document=True, edit_document=False)
    assert set(read_only.functions) == {"google_docs_get_document"}

    create_only, create_service = _connected_tool(
        tmp_path,
        create_document=True,
        read_document=True,
        edit_document=False,
    )
    assert set(create_only.functions) == {"google_docs_get_document", "google_docs_write"}
    create_offered = toolkit_offers(create_only, "google_docs_write")
    assert create_offered.parameters["properties"]["action"]["enum"] == ["create"]
    assert set(create_offered.parameters["properties"]) == {"action", "title", "initial_text"}
    assert create_offered.parameters["required"] == ["action"]
    assert create_offered.description is not None
    assert "- `create`:" in create_offered.description
    assert "- `insert_text`:" not in create_offered.description
    assert "- `batch_update`:" not in create_offered.description
    denied_edit = json.loads(create_only.google_docs_write(action="insert_text", document_id="doc-1", text="hi"))
    assert "disabled" in denied_edit["error"]
    assert create_service.documents_resource.batch_update_calls == []

    edit_only, edit_service = _connected_tool(
        tmp_path,
        create_document=False,
        read_document=False,
        edit_document=True,
    )
    assert set(edit_only.functions) == {"google_docs_write"}
    edit_offered = toolkit_offers(edit_only, "google_docs_write")
    assert edit_offered.parameters["properties"]["action"]["enum"] == ["insert_text", "replace_text", "batch_update"]
    assert "title" not in edit_offered.parameters["properties"]
    assert "initial_text" not in edit_offered.parameters["properties"]
    assert {"action", "document_id", "text", "find_text", "requests", "required_revision_id"} <= set(
        edit_offered.parameters["properties"],
    )
    assert edit_offered.parameters["required"] == ["action"]
    assert edit_offered.description is not None
    assert "- `create`:" not in edit_offered.description
    assert "- `batch_update`:" in edit_offered.description
    denied_create = json.loads(edit_only.google_docs_write(action="create", title="Plan"))
    assert "disabled" in denied_create["error"]
    assert edit_service.documents_resource.create_bodies == []

    # Workspace-prefixed clones also narrow their offered schema.
    rename_toolkit_functions(edit_only, {"google_docs_write": "work_google_docs_write"})
    prefixed_offered = toolkit_offers(edit_only, "work_google_docs_write")
    assert prefixed_offered.parameters["properties"]["action"]["enum"] == [
        "insert_text",
        "replace_text",
        "batch_update",
    ]
    assert "title" not in prefixed_offered.parameters["properties"]


@pytest.mark.parametrize(
    "retired_name",
    [
        "google_docs_create_document",
        "google_docs_insert_text",
        "google_docs_replace_text",
        "google_docs_batch_update",
    ],
)
def test_google_docs_filters_fail_closed_on_retired_names(tmp_path: Path, retired_name: str) -> None:
    for param in ("include_tools", "exclude_tools"):
        with pytest.raises(ValueError, match="google_docs_write"):
            _connected_tool(tmp_path, **{param: [retired_name]})


def test_google_docs_agent_config_accepts_inline_operation_flags() -> None:
    config = Config(
        agents={
            "writer": {
                "display_name": "Writer",
                "tools": [
                    {
                        "google_docs": {
                            "create_document": True,
                            "read_document": True,
                            "edit_document": False,
                        },
                    },
                ],
            },
        },
    )

    docs_config = next(entry for entry in config.resolve_agent("writer").tools.authored if entry.name == "google_docs")

    assert docs_config.tool_config_overrides == {
        "create_document": True,
        "read_document": True,
        "edit_document": False,
    }


def test_google_docs_service_account_env_uses_primary_runtime_auth(tmp_path: Path) -> None:
    service_account_path = tmp_path / "service-account.json"
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(
            tmp_path,
            {"GOOGLE_SERVICE_ACCOUNT_FILE": str(service_account_path)},
        ),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )

    assert tool.service_account_path == str(service_account_path)
    assert tool._should_fallback_to_original_auth() is True
