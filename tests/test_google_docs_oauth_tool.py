"""Tests for the Google Docs OAuth-backed tool."""

# ruff: noqa: D103

from __future__ import annotations

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
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
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


def _connected_tool(tmp_path: Path) -> tuple[GoogleDocsTools, _FakeDocsService]:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
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

    result = json.loads(tool.google_docs_create_document("Launch plan", "First draft"))

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

    result = json.loads(tool.google_docs_create_document("Launch plan", "First draft"))

    assert result["document"]["documentId"] == "created-doc"
    assert result["documentUrl"] == "https://docs.google.com/document/d/created-doc/edit"
    assert "400" in result["initialTextError"]


def test_google_docs_create_document_preserves_id_after_initial_text_transport_failure(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    sentinel = "provider-controlled-transport-secret"
    service.documents_resource.batch_update_error = TimeoutError(sentinel)

    result = json.loads(tool.google_docs_create_document("Launch plan", "First draft"))

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

    result = json.loads(tool.google_docs_insert_text("document-id", "new text", index=6, tab_id="tab-2"))

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

    tool.google_docs_insert_text("document-id", "append", tab_id="tab-2")

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
        tool.google_docs_replace_text(
            "document-id",
            "DRAFT",
            "Final",
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


def test_google_docs_format_document_scopes_supported_operations_to_one_tab_and_revision(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_format_document(
            "document-id",
            "tab-2",
            "revision-1",
            [
                {
                    "type": "paragraph_style",
                    "start_index": 1,
                    "end_index": 8,
                    "style": {"namedStyleType": "HEADING_1"},
                    "fields": ["namedStyleType"],
                },
                {
                    "type": "text_style",
                    "start_index": 1,
                    "end_index": 8,
                    "style": {"bold": True},
                    "fields": ["bold"],
                },
                {
                    "type": "hyperlink",
                    "start_index": 1,
                    "end_index": 8,
                    "url": " https://example.test/plan ",
                },
                {
                    "type": "remove_native_list",
                    "start_index": 19,
                    "end_index": 24,
                },
                {
                    "type": "native_list",
                    "start_index": 9,
                    "end_index": 18,
                    "bullet_preset": "BULLET_DISC_CIRCLE_SQUARE",
                },
            ],
        ),
    )

    assert service.documents_resource.batch_update_calls == [
        {
            "documentId": "document-id",
            "body": {
                "requests": [
                    {
                        "updateParagraphStyle": {
                            "range": {"startIndex": 1, "endIndex": 8, "tabId": "tab-2"},
                            "paragraphStyle": {"namedStyleType": "HEADING_1"},
                            "fields": "namedStyleType",
                        },
                    },
                    {
                        "updateTextStyle": {
                            "range": {"startIndex": 1, "endIndex": 8, "tabId": "tab-2"},
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        },
                    },
                    {
                        "updateTextStyle": {
                            "range": {"startIndex": 1, "endIndex": 8, "tabId": "tab-2"},
                            "textStyle": {"link": {"url": "https://example.test/plan"}},
                            "fields": "link",
                        },
                    },
                    {
                        "deleteParagraphBullets": {
                            "range": {"startIndex": 19, "endIndex": 24, "tabId": "tab-2"},
                        },
                    },
                    {
                        "createParagraphBullets": {
                            "range": {"startIndex": 9, "endIndex": 18, "tabId": "tab-2"},
                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                        },
                    },
                ],
                "writeControl": {"requiredRevisionId": "revision-1"},
            },
        },
    ]
    assert result["tabId"] == "tab-2"


def test_google_docs_format_document_supports_style_resets(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    tool.google_docs_format_document(
        "document-id",
        "tab-2",
        "revision-1",
        [
            {
                "type": "paragraph_style",
                "start_index": 1,
                "end_index": 8,
                "style": {},
                "fields": ["namedStyleType"],
            },
            {
                "type": "text_style",
                "start_index": 1,
                "end_index": 8,
                "style": {},
                "fields": ["bold"],
            },
        ],
    )

    assert service.documents_resource.batch_update_calls[0]["body"]["requests"] == [
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 8, "tabId": "tab-2"},
                "paragraphStyle": {},
                "fields": "namedStyleType",
            },
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 8, "tabId": "tab-2"},
                "textStyle": {},
                "fields": "bold",
            },
        },
    ]


def test_google_docs_format_document_guides_a_400_retry_without_provider_text(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)
    service.documents_resource.batch_update_error = HttpError(
        Response({"status": "400", "reason": "Bad Request"}),
        b'{"error":{"message":"provider-controlled-docs-secret"}}',
    )

    result = tool.google_docs_format_document(
        "document-id",
        "tab-2",
        "revision-1",
        [
            {
                "type": "text_style",
                "start_index": 1,
                "end_index": 8,
                "style": {"bold": True},
                "fields": ["bold"],
            },
        ],
    )

    assert json.loads(result) == {
        "error": (
            "Google Docs request failed (HTTP 400). If the document may have changed, read it again and retry with "
            "its current revisionId; otherwise correct the formatting operation."
        ),
    }
    assert len(service.documents_resource.batch_update_calls) == 1
    assert "provider-controlled" not in result


def test_google_docs_format_document_rejects_unsupported_operations(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_format_document(
            "document-id",
            "tab-2",
            "revision-1",
            [{"type": "delete_content", "start_index": 1, "end_index": 8}],
        ),
    )

    assert result == {"error": "Google Docs formatting operations[0]: operation type is not supported"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_format_document_names_malformed_operation_and_refuses_the_batch(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_format_document(
            "document-id",
            "tab-2",
            "revision-1",
            [
                {
                    "type": "text_style",
                    "start_index": 1,
                    "end_index": 8,
                    "style": {"bold": True},
                    "fields": ["bold"],
                },
                {
                    "type": "text_style",
                    "start_index": 9,
                    "end_index": 18,
                    "style": {"italic": True},
                    "fields": ["italic"],
                    "extra": True,
                },
            ],
        ),
    )

    assert result == {"error": "Google Docs formatting operations[1]: unexpected fields: extra"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_format_document_rejects_link_fields_in_text_style(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_format_document(
            "document-id",
            "tab-2",
            "revision-1",
            [
                {
                    "type": "text_style",
                    "start_index": 1,
                    "end_index": 8,
                    "style": {"bold": True},
                    "fields": ["link.url"],
                },
            ],
        ),
    )

    assert result == {"error": "Google Docs formatting operations[0]: text_style cannot set links; use hyperlink"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_format_document_rejects_grouped_link_fields_in_text_style(tmp_path: Path) -> None:
    tool, service = _connected_tool(tmp_path)

    result = json.loads(
        tool.google_docs_format_document(
            "document-id",
            "tab-2",
            "revision-1",
            [
                {
                    "type": "text_style",
                    "start_index": 1,
                    "end_index": 8,
                    "style": {"bold": True},
                    "fields": ["link(url)"],
                },
            ],
        ),
    )

    assert result == {"error": "Google Docs formatting operations[0]: style fields must be explicit dotted paths"}
    assert service.documents_resource.batch_update_calls == []


def test_google_docs_config_flags_control_registered_functions(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        create_document=False,
        read_document=True,
        edit_document=False,
        format_document=True,
    )

    assert set(tool.functions) == {"google_docs_format_document", "google_docs_get_document"}


def test_google_docs_format_operation_is_disabled_by_default(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
    )

    assert "google_docs_format_document" not in tool.functions


def test_google_docs_format_operation_requires_document_reads(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="format_document requires read_document"):
        GoogleDocsTools(
            runtime_paths=_runtime_paths(tmp_path),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            worker_target=None,
            creds=_valid_credentials(),
            read_document=False,
            format_document=True,
        )


def test_google_docs_format_operation_declares_supported_operation_shapes(tmp_path: Path) -> None:
    tool = GoogleDocsTools(
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
        creds=_valid_credentials(),
        format_document=True,
    )

    function = toolkit_function(tool, "google_docs_format_document")

    assert function is not None
    parameters = function.parameters
    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["document_id", "tab_id", "expected_revision_id", "operations"]
    operations = parameters["properties"]["operations"]
    assert operations["minItems"] == 1
    schemas = operations["items"]["oneOf"]
    assert {schema["properties"]["type"]["enum"][0] for schema in schemas} == {
        "paragraph_style",
        "text_style",
        "native_list",
        "remove_native_list",
        "hyperlink",
    }
    assert all(schema["additionalProperties"] is False for schema in schemas)


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
                            "format_document": True,
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
        "format_document": True,
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
